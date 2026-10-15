"""Read the separate ES/NQ/MNQ feed tables and persist automatic research plans.
No AI calls, broker connections, orders or fill simulation. Selection runs on a
frozen receipt cutoff; late arrivals cannot rewrite a recorded decision.
"""
import json
import time
from pathlib import Path
from dataclasses import asdict
from auto_selector import select,Policy,VERSION
from data_health import account_snapshot
from risk_sizing import risk_budget
from management_policy import contract as management_contract
from manual_account import snapshot_current
from ai_entry_gate import evaluate, apply, VERSION as GATE_VERSION, TTL_SECONDS


from scheduled_news import NewsCalendar, apply_news, VERSION as NEWS_VERSION
from account_guard import assess as assess_account, apply_account, VERSION as ACCOUNT_VERSION
from source_rules import contract as source_contract, require_source_selection
from daily_atr import Policy as ATRPolicy,calculate as calculate_atr,VERSION as ATR_VERSION
from context_watch import evaluate as evaluate_watch,VERSION as WATCH_VERSION


class SelectionObserver:
    def __init__(self,store,config):
        self.store=store;self.policy=Policy();self.news=NewsCalendar(store)
        self.atr_policy=ATRPolicy.from_config(config)
        self.strategy_mode=config.get('TANJA_STRATEGY_POLICY','source_selections')
        if self.strategy_mode not in ('source_selections','legacy_user_core'):
            raise ValueError('Unknown Tanja strategy policy')
        if self.strategy_mode=='source_selections': self.policy=Policy(discovery_only=True,timeframe_priority=(1,2,3,5))
        self.enabled=config.get('TANJA_AUTO_SELECTION_ENABLED','true').lower()=='true'
        self.entry_selection_mode=config.get('TANJA_ENTRY_SELECTION_MODE','context_rules')
        if self.entry_selection_mode not in ('context_rules','individual_trades'):
            raise ValueError('Unknown entry selection mode')
        self.max_contracts=int(config.get('TANJA_ACCOUNT_MAX_MNQ','40'))
        if not 1<=self.max_contracts<=40:
            raise ValueError('Invalid hypothetical automatic-selection risk configuration')
        self.configuration=dict(management=management_contract(),confirmation_policy="closed_m5_v1",news_version=NEWS_VERSION,account_guard_version=ACCOUNT_VERSION,ai_gate_version=GATE_VERSION,ai_context_ttl_seconds=TTL_SECONDS,version=VERSION,policy=asdict(self.policy),sizing_version='daily-reported-balance-v2',risk_fraction=.005,risk_basis='daily_reported_balance',paper_max_contracts=self.max_contracts,costs_included=False)
        self.configuration.update(strategy_mode=self.strategy_mode, source_policy=source_contract())
        self.configuration.update(daily_atr_version=ATR_VERSION,daily_atr_policy=asdict(self.atr_policy),context_watch_version=WATCH_VERSION,analysis_symbol=store.analysis_symbol)
        from setup_scanner import VERSION as SCANNER_VERSION
        self.configuration.update(entry_selection_mode=self.entry_selection_mode,sequence_scanner_version=SCANNER_VERSION)
        if self.strategy_mode=='source_selections':
            self.configuration.update(management=source_contract(), confirmation_policy='setup_specific_explicit_review')
        from entry_rules import fingerprint
        self.revision=fingerprint(self.configuration)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS auto_selections(
              revision TEXT,cutoff INTEGER,frozen_at INTEGER,status TEXT,payload TEXT,
              PRIMARY KEY(revision,cutoff))''')

    def sizing_context(self,now=None):
        now=time.time() if now is None else now
        snapshot=account_snapshot(Path(self.store.path).parent)
        if snapshot is None:
            return dict(state='BALANCE_REQUIRED',risk_fraction=.005,budget_usd=None,broker_verified=False)
        if not snapshot_current(snapshot,now):
            return dict(state='DAILY_BALANCE_UPDATE_REQUIRED',risk_fraction=.005,budget_usd=None,broker_verified=False,
                        balance=snapshot['balance'],balance_as_of=snapshot['as_of'])
        return dict(state='MANUAL_BALANCE_RESEARCH_ONLY',risk_fraction=.005,budget_usd=risk_budget(snapshot['balance']),
                    balance=snapshot['balance'],balance_as_of=snapshot['as_of'],broker_verified=False,
                    costs_included=False,max_contracts=self.max_contracts)

    def process_one(self,now=None,source_review=None):
        if not self.enabled:return False
        frozen=int(time.time() if now is None else now)
        with self.store.connect() as db:
            # Consistent read snapshot; compute outside the transaction so feed
            # writes are not held behind potentially expensive feature work.
            db.execute('BEGIN')
            latest=[]
            for table,sym in [('bars','ES'),('bars','MNQ'),('collection_bars','NQ')]:
                row=db.execute(f'SELECT MAX(end) FROM {table} WHERE symbol=? AND received<=?',(sym,frozen)).fetchone()
                if row[0] is None:return False
                latest.append(row[0])
            cutoff=min(latest)
            if db.execute('SELECT 1 FROM auto_selections WHERE revision=? AND cutoff=?',(self.revision,cutoff)).fetchone():return False
            # Read the latest attempt in the same transaction as the candles.
            # A newer wait/failure supersedes older approval; never search back
            # for a more convenient directional answer.
            ai_row = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_runs'").fetchone():
                found=db.execute('SELECT * FROM ai_runs WHERE started<=? ORDER BY started DESC,id DESC LIMIT 1',(frozen,)).fetchone()
                if found:ai_row=dict(found)
            markets={}
            for table,sym in [('bars','ES'),('bars','MNQ'),('collection_bars','NQ')]:
                rows=db.execute(f'SELECT payload,received FROM {table} WHERE symbol=? AND end<=? AND received<=? ORDER BY start DESC LIMIT 12000',(sym,cutoff,frozen)).fetchall()
                markets[sym]=[]
                for row in reversed(rows):
                    b=json.loads(row['payload'])
                    markets[sym].append(dict(time=b['bar_open_ms']//1000,received_at=row['received'],**{k:b[k] for k in ('open','high','low','close')}))
        # Do not replay missed minutes after downtime as fresh decisions.
        sizing=self.sizing_context(frozen)
        account=assess_account(account_snapshot(Path(self.store.path).parent),frozen,self.max_contracts)
        news=self.news.state(frozen)
        budget=account['budget_usd'] if account['state']=='REPORTED_LIMITS_CLEAR' else sizing['budget_usd']
        cap=account['max_contracts'] if account['state']=='REPORTED_LIMITS_CLEAR' else self.max_contracts
        sizing.update(available_budget_usd=account['budget_usd'],available_contracts=account['max_contracts'])
        atr=calculate_atr(markets['MNQ'],cutoff=cutoff,now=frozen,policy=self.atr_policy)
        try:
            if sizing['budget_usd'] is None or sizing['budget_usd']<=0:
                result=dict(state='ABSTAIN',reasons=['DAILY_BALANCE_UPDATE_REQUIRED' if sizing['state']=='DAILY_BALANCE_UPDATE_REQUIRED' else 'CURRENT_BALANCE_REQUIRED'],orders_enabled=False,semantic_fidelity_verified=False,cutoff=cutoff)
            else:
                result=select(markets,cutoff=cutoff,now=frozen,risk_budget_usd=budget,max_contracts=cap,policy=self.policy)
        except (ValueError,TypeError,KeyError) as exc:
            result=dict(state='INPUT_REJECTED',reasons=[type(exc).__name__],orders_enabled=False,semantic_fidelity_verified=False,cutoff=cutoff)
        source_selected=False
        watched=None
        if self.strategy_mode=='source_selections' and source_review is None and budget and cap:
            from entry_review import from_saved_review
            try:
                watched=(evaluate_watch(ai_row,markets,cutoff=cutoff,now=frozen,budget=budget,cap=cap,daily_atr=atr,analysis_symbol=self.store.analysis_symbol)
                    if self.entry_selection_mode=='context_rules' else None)
                reviewed=watched if watched is not None else from_saved_review(ai_row,now=frozen,budget=budget,cap=cap)
                if reviewed is not None:
                    result=reviewed
                    result['cutoff']=cutoff
                    source_selected=bool(result.get('compiled',{}).get('plan'))
            except (ValueError,TypeError,KeyError,IndexError):
                result.update(state='COMPILER_BLOCKED',reasons=['SAVED_ENTRY_REVIEW_FAILED_VALIDATION'])
                if result.get('compiled'):result['compiled']['plan']=None
        if self.strategy_mode=='source_selections' and source_review is not None and result.get('packet'):
            from entry_rules import compile_entry
            from source_rules import VERSION as SOURCE_VERSION
            s=source_review.get('selection',{});observations=source_review.get('observations',[])
            if s.get('confirmation_policy')!=SOURCE_VERSION: raise ValueError('Source selection policy required')
            compiled=compile_entry(result['packet'],s,observations,now=frozen)
            source_audit=dict(recipe=s.get('setup_profile','unresolved'),poi='explicit_context_review',timeframe=s.get('timeframe_minutes'),
                stop=(compiled.get('price_review') or {}).get('stop'),target=dict(price=(compiled.get('price_review') or {}).get('target'),reason=s.get('target',{}).get('reason','Unresolved')),
                quantity=s.get('initial_quantity'),selection_origin='explicit_context_review')
            result.update(compiled=compiled,selected=dict(selection=s,observations=observations,audit=source_audit),
                state='PLAN_READY' if compiled['plan'] else 'COMPILER_BLOCKED',reasons=compiled['missing']+compiled['errors'])
            source_selected=bool(compiled['plan'])
        result=apply(result,watched['ai_context'] if watched is not None else evaluate(ai_row,markets,cutoff=cutoff,now=frozen))
        result=apply_account(apply_news(result,news),account)
        for candidate in result.get('candidate_evaluations',[]):
            if candidate['state']=='PLAN_READY' and result['state']!='PLAN_READY':
                candidate.update(state=result['state'],reasons=result['reasons'])
        if self.strategy_mode=='source_selections' and not source_selected: result=require_source_selection(result)
        result['management_policy']=source_contract() if self.strategy_mode=='source_selections' else management_contract()
        if result.get('compiled',{}).get('plan'):
            result['compiled']['plan']['management_policy']=result['management_policy']
        result.update(frozen_at=frozen,revision=self.revision,configuration=self.configuration,sizing=sizing)
        result['daily_atr']=atr
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            inserted=db.execute('INSERT OR IGNORE INTO auto_selections VALUES(?,?,?,?,?)',(self.revision,cutoff,frozen,result['state'],json.dumps(result,allow_nan=False))).rowcount
            # Bounded research retention; not a permanent trading ledger.
            db.execute('DELETE FROM auto_selections WHERE rowid NOT IN (SELECT rowid FROM auto_selections ORDER BY frozen_at DESC,rowid DESC LIMIT 100)')

        return bool(inserted)

    def state(self):
        with self.store.connect() as db:
            rows=db.execute('SELECT payload FROM auto_selections ORDER BY frozen_at DESC,rowid DESC LIMIT 30').fetchall()
        records=[]
        for row in rows:
            result=json.loads(row[0]);result.pop('packet',None)
            # Keep heavy frozen evidence in authenticated audit, not every poll.
            if result.get('compiled'):result['compiled'].pop('review_observations',None)
            records.append(result)
        return dict(daily_atr=records[0].get('daily_atr') if records else None,scheduled_news=self.news.state(time.time()),account_guard=assess_account(account_snapshot(Path(self.store.path).parent),time.time(),self.max_contracts),enabled=self.enabled,configuration=self.configuration,revision=self.revision,records=records,sizing=self.sizing_context(),
          mode='AUTOMATIC_RESEARCH_ONLY',orders_enabled=False,extra_ai_calls=0,automatic_paper_fills=False,
          note='Deterministic long/short research policies select from ES/NQ and price on MNQ. Numerical choices are unvalidated proxies, not proven Tanja rules. Risk/quantity are hypothetical settings, not a Builder-account authorization.')

    def audit(self,revision,cutoff):
        with self.store.connect() as db:
            row=db.execute('SELECT payload FROM auto_selections WHERE revision=? AND cutoff=?',(revision,cutoff)).fetchone()
        return json.loads(row[0]) if row else None

    def run(self,stop):
        while not stop.is_set():
            try:self.process_one()
            except Exception:self.store.note('automatic_selection','Automatic selection unavailable; inspect service status.')
            stop.wait(2)
