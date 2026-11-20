"""Explicit paper bridge into the existing durable simulation and independent risk.
No broker, live execution, implicit fill, quote invention, or production policy reuse.
"""
import json
from pathlib import Path
from .common import digest,encode
from .simulator import BrokerSimulator
from .risk import Policy

class PaperSession:
    def __init__(self,observer_store,journal,policy_file):
        self.observer=observer_store
        raw=json.loads(Path(policy_file).read_text())
        if raw.get('mode')!='PAPER' or not raw.get('approval_id'):raise ValueError('EXPLICIT_PAPER_POLICY_REQUIRED')
        self.policy=Policy(**raw['risk']);self.approval_id=raw['approval_id'];self.sim=BrokerSimulator(journal)
        with __import__('sqlite3').connect(self.sim.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS paper_decisions(id TEXT PRIMARY KEY,snapshot_id TEXT,payload TEXT)')
    def reconcile(self,at,positions):return self.sim.reconcile(at,positions)
    def consider(self,snapshot_id,setup_id,account,market,now):
        snap=self.observer.snapshot(snapshot_id)
        if not snap or snap['as_of']>now:raise ValueError('UNKNOWN_OR_FUTURE_SNAPSHOT')
        setup=next((s for s in snap['setups'] if s['id']==setup_id),None)
        if not setup or setup['state']!='READY' or not setup.get('plan'):raise ValueError('NO_RECORDED_RESEARCH_PLAN')
        # Existing policy accepts only observed plan geometry; user-supplied entry
        # numbers cannot replace the immutable recorded proposal at this boundary.
        plan=dict(setup['plan'],symbol=setup['symbol'],contract=setup['contract'],direction=setup['direction'])
        result=self.sim.submit(setup_id,plan,self.policy,account,market,now)
        item=dict(snapshot_id=snapshot_id,setup_id=setup_id,approval_id=self.approval_id,result=result,at=now,mode='PAPER',orders_sent=0)
        ident=digest(item)
        with __import__('sqlite3').connect(self.sim.path) as db:db.execute('INSERT OR IGNORE INTO paper_decisions VALUES(?,?,?)',(ident,snapshot_id,encode(item)))
        return result
    def event(self,*args,**kwargs):
        # Caller must provide timestamped simulated ACK/fill/protection/exit events.
        # The protocol engine never converts an OHLC touch to an actual fill.
        return self.sim.event(*args,**kwargs)
