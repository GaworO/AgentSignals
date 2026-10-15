"""Continuous quote-driven paper loop and durable broker-intent boundary.

The installed TradersPost transport cannot reconcile or atomically resize a
bracket after a partial. Broker mode therefore stays blocked for that adapter.
No OHLC candle, HTTP receipt or simulated acknowledgement becomes a real fill.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import math
from pathlib import Path
import time
from paper_lifecycle import PaperLifecycle,gated_plan
from broker_feedback import encode

VERSION='tanja-continuous-loop-v1'


class TradingLoop:
    def __init__(self,store,observer,feedback,config):
        self.store=store;self.observer=observer;self.feedback=feedback
        self.mode=config.get('TANJA_TRADING_LOOP_MODE','off')
        self.require_market_readiness=config.get('TANJA_REQUIRE_MARKET_READINESS','true').lower()=='true'
        self.market_readiness=None
        if self.mode not in ('off','paper','broker'):raise ValueError('Unknown trading loop mode')
        self.slippage=float(config.get('TANJA_PAPER_SLIPPAGE_POINTS','0.25'))
        self.fee=float(config.get('TANJA_FEE_PER_CONTRACT_SIDE','0.50'))
        if not math.isfinite(self.slippage) or not math.isfinite(self.fee) or min(self.slippage,self.fee)<0 or self.slippage*4!=round(self.slippage*4):raise ValueError('Explicit tick slippage and fee required')
        self.sim=PaperLifecycle(Path(store.path).parent/'continuous-paper.sqlite3',slippage_points=self.slippage,fee_per_side=self.fee) if self.mode=='paper' else None
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS execution_intents(
                id TEXT PRIMARY KEY,plan_id TEXT,kind TEXT,created INTEGER,status TEXT,payload TEXT,receipt TEXT,
                UNIQUE(plan_id,kind));
                CREATE TABLE IF NOT EXISTS loop_status(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT);''')
            db.execute('CREATE TABLE IF NOT EXISTS loop_quote_events(id TEXT PRIMARY KEY,payload TEXT,done INTEGER)')
        self.status='DISABLED' if self.mode=='off' else 'WAITING_FOR_LIVE_QUOTES'
        self.reason='No trading loop enabled.' if self.mode=='off' else 'A broker bridge must provide fresh quotes.'

    def save_status(self,status,reason,now):
        self.status=status;self.reason=reason
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO loop_status VALUES(1,?)',(encode(dict(status=status,reason=reason,at=now)),))
        return False

    def process_one(self,now=None):
        now=int(time.time() if now is None else now)
        if self.mode=='off':return False
        f=self.feedback.state(now)
        if not f['quotes_current']:return self.save_status('LIVE_QUOTES_REQUIRED','Need fresh ES, NQ and exact-contract MNQ quotes; closed minute bars do not substitute.',now)
        if self.mode=='broker':
            if not f['account_verified']:return self.save_status('BROKER_FEEDBACK_REQUIRED','Need current broker orders, fills, positions and complete account-wide trade history.',now)
            if f['loss_count']>=2:return self.save_status('TWO_ACCOUNT_LOSSES_NEW_YORK_DAY','Two net losing completed trades across this account block new entries; break-even trades do not count.',now)
            if self.require_market_readiness:
                if self.market_readiness is None:return self.save_status('MARKET_READINESS_REQUIRED','Production feeds and exact contract mapping must be checked before broker entries.',now)
                readiness=self.market_readiness.state(now)
                reasons=readiness['entry_reasons']+self.market_readiness.quote_reasons(f['quotes'],now)
                if reasons:return self.save_status('MARKET_DATA_BLOCKED','; '.join(reasons),now)
            return self.save_status('BROKER_MANAGEMENT_ADAPTER_REQUIRED','The TradersPost webhook cannot provide atomic partial/bracket resizing or order-ID amendments. Automatic broker entries remain blocked.',now)
        quote=f['quotes']['MNQ'];value=quote['data']['last'];at=quote['observed_at']
        with self.store.connect() as db:
            processed=db.execute('SELECT payload,done FROM loop_quote_events WHERE id=?',(quote['event_id'],)).fetchone()
        if processed and processed['done']:return False
        state=self.sim.snapshot();active=next((r for r in state['runs'] if r['state'] in ('OPEN','ENTRY_PENDING')),None)
        if now<=state['cursor']:return False
        from account_guard import assess
        from data_health import account_snapshot
        account=assess(account_snapshot(Path(self.store.path).parent),now,self.observer.max_contracts)
        news=self.observer.news.state(now)
        entry_reasons=list(account['reasons'])+list(news.get('reasons',[]))
        if self.require_market_readiness and (active is None or active['state']=='ENTRY_PENDING'):
            if self.market_readiness is None:entry_reasons.append('MARKET_READINESS_REQUIRED')
            else:
                entry_reasons.extend(self.market_readiness.state(now)['entry_reasons'])
                entry_reasons.extend(self.market_readiness.quote_reasons(f['quotes'],now))
        if f['loss_count'] is not None and f['loss_count']>=2:entry_reasons.append('TWO_ACCOUNT_LOSSES_NEW_YORK_DAY')
        if f.get('entry_accounting_required') and not f.get('accounting',{}).get('accounting_ready'):
            entry_reasons.extend(f.get('accounting',{}).get('reasons') or ['ACCOUNTING_RECONCILIATION_REQUIRED'])
        if active is None:
            records=self.observer.state()['records']
            latest=next((r for r in records if r.get('revision')==self.observer.revision),None)
            if latest and latest['state']=='PLAN_READY':
                decision=self.observer.audit(latest['revision'],latest['cutoff'])
                plan=decision['compiled']['plan']
                consumed={r['id'] for r in state['runs']}
                used_setups={r['decision']['compiled']['plan'].get('setup_id') for r in state['runs']}
                if plan.get('setup_id') and plan['setup_id'] in used_setups:
                    return self.save_status('SETUP_ALREADY_ATTEMPTED','A prior entry attempt consumed this selected structure or HTF gap/objective; a new inversion does not create a reentry permission.',now)
                if plan['plan_id'] not in consumed:
                    if news.get('context_not_before') and (decision['ai_context'].get('source_cutoff') or 0)<news['context_not_before']:entry_reasons.append('NEWS_REQUIRES_FRESH_AI_CONTEXT')
                    if entry_reasons:return self.save_status('PAPER_ENTRY_BLOCKED','; '.join(entry_reasons),now)
                    decision['account_guard']=account
                    decision['scheduled_news']=news
                    try:
                        self.sim.submit('continuous-submit:'+plan['plan_id'],decision=decision,now=now)
                        self.save_status('PAPER_ENTRY_PENDING','Research entry reserved; waiting for a later live quote to simulate a fill.',now)
                        return True
                    except ValueError as exc:return self.save_status('PAPER_ENTRY_BLOCKED',str(exc),now)
        if active and news.get('context_not_before') and (active['decision']['ai_context'].get('source_cutoff') or 0)<news['context_not_before']:entry_reasons.append('NEWS_REQUIRES_FRESH_AI_CONTEXT')
        if news.get('state')!='CLEAR':
            # Native protection and queued risk reductions still run. Unknown
            # news forbids pending entries by conservatively cancelling them.
            events=[dict(at=now,known_at=now)]
        else:events=[dict(at=int(e['at']),known_at=int(e['known_at'])) for e in news.get('events',[])]
        es=f['quotes']['ES'];nq=f['quotes']['NQ']
        bars=self.store.history('MNQ',now//60*60,now)
        rows=[dict(time=b['bar_open_ms']//1000,**{k:b[k] for k in ('open','high','low','close')}) for b in bars]
        from daily_atr import calculate
        atr=calculate(bars,cutoff=now//60*60,now=now,policy=self.observer.atr_policy)
        args=dict(at=at,received_at=now,market_price=value,mnq_bars=rows,news_events=events,daily_atr=atr,entry_block_reasons=entry_reasons,
            es_quote=dict(at=es['observed_at'],price=es['data']['last']),nq_quote=dict(at=nq['observed_at'],price=nq['data']['last']))
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO loop_quote_events VALUES(?,?,0)',(quote['event_id'],encode(args)))
            args=json.loads(db.execute('SELECT payload FROM loop_quote_events WHERE id=?',(quote['event_id'],)).fetchone()[0])
        result=self.sim.quote('continuous-quote:'+quote['event_id'],**args)
        with self.store.connect() as db:db.execute('UPDATE loop_quote_events SET done=1 WHERE id=?',(quote['event_id'],))
        self.save_status('PAPER_LOOP_RUNNING','Live-quote-driven simulated fills only; no broker orders sent.',now)
        return result

    def prepare_entry(self,decision,now):
        """Reviewable draft only. No transport, fill assumption or broker state."""
        plan,review=gated_plan(decision,now)
        contract=self.feedback.contract
        if not contract:raise ValueError('EXPLICIT_EXECUTION_CONTRACT_REQUIRED')
        iso=lambda ts:datetime.fromtimestamp(ts,ZoneInfo('UTC')).isoformat().replace('+00:00','Z')
        identity=hashlib.sha256((VERSION+':'+plan['plan_id']+':entry').encode()).hexdigest()
        payload=dict(ticker=contract,action='buy' if plan['direction']=='long' else 'sell',
            sentiment='bullish' if plan['direction']=='long' else 'bearish',quantity=plan['initial_quantity'],quantityType='fixed_quantity',
            orderType='limit' if plan['entry_mode']=='retracement' else 'market',time=iso(now),expiresAt=iso(plan['context_valid_until']),
            stopLoss=dict(type='stop',stopPrice=review['stop']),takeProfit=dict(limitPrice=review['target']),
            extras=dict(source=VERSION,intent_id=identity,plan_id=plan['plan_id']),test=True)
        if plan['entry_mode']=='retracement':payload['limitPrice']=plan['entry_reference']['price']
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT payload FROM execution_intents WHERE id=?',(identity,)).fetchone()
            if old:
                draft=json.loads(old[0])
                if draft['ticker']!=contract:raise ValueError('DRAFT_BELONGS_TO_ANOTHER_EXECUTION_CONTRACT')
                return draft
            db.execute('INSERT INTO execution_intents VALUES(?,?,?,?,?,?,NULL)',(identity,plan['plan_id'],'ENTRY',now,'draft_only',encode(payload)))
        return payload

    def state(self,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            status=db.execute('SELECT payload FROM loop_status WHERE id=1').fetchone()
            intents=[dict(r) for r in db.execute('SELECT id,plan_id,kind,created,status,payload,receipt FROM execution_intents ORDER BY created DESC LIMIT 20')]
        for r in intents:
            for k in ('payload','receipt'):r[k]=json.loads(r[k] or 'null')
        return dict(version=VERSION,mode=self.mode,orders_enabled=False,broker_loop_connected=False,
            **(json.loads(status[0]) if status else dict(status=self.status,reason=self.reason)),
            feedback=self.feedback.state(now),paper=self.sim.snapshot() if self.sim else None,intents=intents,
            blockers=['Broker feedback bridge is not supplied by a TradersPost webhook.','TradersPost partial exits require a separate verified protection-resize mechanism.'])

    def run(self,stop):
        while not stop.is_set():
            try:self.process_one()
            except Exception:self.save_status('LOOP_CHECK_FAILED','Inspect loop input/reconciliation; no broker request was sent.',int(time.time()))
            stop.wait(.25)
