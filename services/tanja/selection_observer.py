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


class SelectionObserver:
    def __init__(self,store,config):
        self.store=store;self.policy=Policy()
        self.enabled=config.get('TANJA_AUTO_SELECTION_ENABLED','true').lower()=='true'
        self.max_contracts=int(config.get('TANJA_ACCOUNT_MAX_MNQ','40'))
        if not 1<=self.max_contracts<=40:
            raise ValueError('Invalid hypothetical automatic-selection risk configuration')
        self.configuration=dict(version=VERSION,policy=asdict(self.policy),sizing_version='balance-fraction-v1',risk_fraction=.005,risk_basis='current_account_balance',paper_max_contracts=self.max_contracts,costs_included=False)
        from entry_rules import fingerprint
        self.revision=fingerprint(self.configuration)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS auto_selections(
              revision TEXT,cutoff INTEGER,frozen_at INTEGER,status TEXT,payload TEXT,
              PRIMARY KEY(revision,cutoff))''')

    def sizing_context(self):
        snapshot=account_snapshot(Path(self.store.path).parent)
        if snapshot is None:
            return dict(state='BALANCE_REQUIRED',risk_fraction=.005,budget_usd=None,broker_verified=False)
        return dict(state='MANUAL_BALANCE_RESEARCH_ONLY',risk_fraction=.005,budget_usd=risk_budget(snapshot['balance']),
                    balance=snapshot['balance'],balance_as_of=snapshot['as_of'],broker_verified=False,
                    costs_included=False,max_contracts=self.max_contracts)

    def process_one(self,now=None):
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
            markets={}
            for table,sym in [('bars','ES'),('bars','MNQ'),('collection_bars','NQ')]:
                rows=db.execute(f'SELECT payload,received FROM {table} WHERE symbol=? AND end<=? AND received<=? ORDER BY start DESC LIMIT 12000',(sym,cutoff,frozen)).fetchall()
                markets[sym]=[]
                for row in reversed(rows):
                    b=json.loads(row['payload'])
                    markets[sym].append(dict(time=b['bar_open_ms']//1000,received_at=row['received'],**{k:b[k] for k in ('open','high','low','close')}))
        # Do not replay missed minutes after downtime as fresh decisions.
        sizing=self.sizing_context()
        try:
            if sizing['budget_usd'] is None or sizing['budget_usd']<=0:
                result=dict(state='ABSTAIN',reasons=['CURRENT_BALANCE_REQUIRED'],orders_enabled=False,semantic_fidelity_verified=False,cutoff=cutoff)
            else:
                result=select(markets,cutoff=cutoff,now=frozen,risk_budget_usd=sizing['budget_usd'],max_contracts=self.max_contracts,policy=self.policy)
        except (ValueError,TypeError,KeyError) as exc:
            result=dict(state='INPUT_REJECTED',reasons=[type(exc).__name__],orders_enabled=False,semantic_fidelity_verified=False,cutoff=cutoff)
        result.update(frozen_at=frozen,revision=self.revision,configuration=self.configuration,sizing=sizing)
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
        return dict(enabled=self.enabled,configuration=self.configuration,revision=self.revision,records=records,sizing=self.sizing_context(),
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
