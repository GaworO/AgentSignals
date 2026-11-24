"""One opt-in observer writer inside Agent50k; read-only existing intake, no broker."""
import fcntl
import json
import threading
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from .common import save,digest,code_hash
from .store import Store
from .bridge import observe_once
from .storage_guard import WriteGuard,WritePaused

def review_gate(snapshot,cfg,now,last_requested):
    """An explicit dated NY window, freshness and meaningful-event pacing."""
    ny=datetime.fromtimestamp(now,ZoneInfo('America/New_York'))
    if ny.date().isoformat()!=cfg.get('ICT_AI_SESSION_DATE'):return 'OUTSIDE_AUTHORIZED_DATE'
    minute=ny.hour*60+ny.minute
    if not 570<=minute<720:return 'OUTSIDE_NYAM'
    if not snapshot or not 0<=now-snapshot['as_of']<=90:return 'STALE_OR_EMPTY'
    if now-last_requested<max(180,float(cfg.get('ICT_AI_MIN_INTERVAL_SECONDS',180))):return 'EVENT_COOLDOWN'
    relevant=any(e.get('timeframe',0)>=5 and e.get('kind') in ('SWEEP','STRUCTURE_BREAK','DISPLACEMENT','FVG','IFVG','DATA_GAP','CONTRACT_CHANGE') for e in snapshot['events'])
    if not relevant and last_requested and now-last_requested<900:return 'NO_SIGNIFICANT_CHANGE'
    return 'READY'

class Worker:
    def __init__(self,cfg):
        self.cfg=dict(cfg);self.root=Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai'
        self.mode=cfg.get('ICT_AI_MODE','OFF');self.thread=None;self.stop_event=threading.Event();self.wake=threading.Event()
        self.write_guard=WriteGuard(cfg);self.current_status=None;self._last_log=None
        self.max_age_seconds=int(cfg.get('ICT_AI_CATCHUP_SECONDS',300))
        self.max_batches=int(cfg.get('ICT_AI_MAX_BATCHES_PER_POLL',5))
        if not 60<=self.max_age_seconds<=3600 or not 1<=self.max_batches<=60:
            raise ValueError('LIVE_CATCHUP_LIMITS_INVALID')
    def notify(self):self.wake.set()
    def status(self,**values):
        body=dict(at=time.time(),mode=self.mode,orders_enabled=False,**values)
        self.current_status=body
        try:
            self.write_guard()
            save(self.root/'worker-status.json',body)
        except (WritePaused,OSError) as exc:
            # Even the status writer must pause. Keep HTTP status in memory and
            # log to stdout; failure to report must not kill the observer thread.
            if isinstance(exc,WritePaused):body.update(state='PAUSED_WRITES',storage=exc.evidence)
            else:body.update(state='PAUSED_WRITES',error=type(exc).__name__)
            key=(body['state'],body.get('error'),body.get('storage',{}).get('reason'))
            if key!=self._last_log:
                print('[ict-ai-observer] '+json.dumps(body),flush=True);self._last_log=key
    def start(self):
        if self.mode not in ('OBSERVE','SHADOW_AI'):return
        if not self.thread or not self.thread.is_alive():
            self.thread=threading.Thread(target=self.run,name='ict-ai-observer',daemon=True);self.thread.start()
    def stop(self):self.stop_event.set();self.wake.set()
    def run(self):
        # No mkdir, lock, audit, database or status file until storage is safe.
        while not self.stop_event.is_set():
            try:self.write_guard();break
            except WritePaused as exc:
                self.status(state='PAUSED_WRITES',storage=exc.evidence)
                self.stop_event.wait(2)
        if self.stop_event.is_set():return
        self.root.mkdir(parents=True,exist_ok=True)
        lock=(self.root/'observer.lock').open('a+')
        try:
            while not self.stop_event.is_set():
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:self.stop_event.wait(15)
            if self.stop_event.is_set():return
            from .knowledge import Knowledge
            from .observer import Observer
            source=Path(self.cfg.get('ICT_AI_SOURCE_DB',self.root.parent/'market_context/tanja.sqlite3')).resolve()
            target=Path(self.cfg.get('ICT_AI_OBSERVE_DB',self.root/'observe.sqlite3')).resolve()
            if target==source:raise ValueError('SOURCE_AND_OBSERVER_MUST_DIFFER')
            if not source.is_file():raise ValueError('EXISTING_MARKET_SOURCE_REQUIRED')
            self.write_guard()
            self.status(state='AUDITING_KNOWLEDGE')
            knowledge=Knowledge(corpus=self.cfg.get('ICT_AI_CORPUS_DIR'),atlas=self.cfg.get('ICT_AI_ATLAS_DIR'))
            self.write_guard()
            if hasattr(knowledge,'audit'):save(self.root/'knowledge-audit.json',knowledge.audit)
            if self.stop_event.is_set():return
            self.status(state='HASHING_CODE')
            version=code_hash()
            if self.stop_event.is_set():return
            manifest=dict(version='agent50k-ict-observer-v3',knowledge_version=knowledge.version,index_version=knowledge.index_version,
                source=str(source),code_hash=version,mode='READ_ONLY_OBSERVER',orders_enabled=False,
                intake_policy=dict(kind='BOUNDED_RECENT_WINDOW',max_age_seconds=self.max_age_seconds,
                                   max_batches_per_poll=self.max_batches,older_history='NOT_REPLAYED'))
            store=Store(target,manifest,write_guard=self.write_guard);observer=Observer(store,knowledge);last_review=-1
            ledger=None;last_requested=0
            if self.mode=='SHADOW_AI':
                ledger=Store(self.cfg.get('ICT_AI_REASONING_DB',target),write_guard=self.write_guard)
                from .reasoning import initialize
                initialize(ledger)
                with ledger.readonly() as db:last_requested=db.execute('SELECT coalesce(max(requested_at),0) FROM reasoning_reviews').fetchone()[0]
            while not self.stop_event.is_set():
                self.wake.clear()
                try:
                    storage=self.write_guard()
                    result=observe_once(observer,source,time.time(),max_age_seconds=self.max_age_seconds,max_batches=self.max_batches);snapshot=store.snapshot()
                    review_status=review_gate(snapshot,self.cfg,time.time(),last_requested) if self.mode=='SHADOW_AI' else 'NOT_ENABLED'
                    if review_status=='READY' and snapshot['as_of']>last_review:
                        from .reasoning import review
                        try:
                            # A fresh event/reassessment snapshot triggers one bounded
                            # review. Historical catch-up never triggers paid requests.
                            last_requested=time.time()
                            r=review(store,snapshot['id'],self.cfg,knowledge=knowledge,ledger_store=ledger);review_status=r['status']
                        except ValueError as exc:review_status=str(exc)
                        last_review=snapshot['as_of']
                    self.status(state='OBSERVING',processed=result['processed'],missing_pair_count=len(result['missing_pairs']),
                        late_count=len(result['late']),latest_as_of=snapshot['as_of'] if snapshot else None,
                        data_fresh=bool(snapshot and 0<=time.time()-snapshot['as_of']<=120),llm=review_status,
                        storage=storage,intake_after_start=result['intake_after_start'],
                        history_not_replayed_before=result['history_not_replayed_before'])
                    self.wake.wait(2)
                except WritePaused as exc:
                    self.status(state='PAUSED_WRITES',storage=exc.evidence)
                    self.wake.wait(2)
                except Exception as exc:
                    # No skip, no fabricated bars, no retry of uncertain paid calls.
                    self.status(state='BLOCKED',error=type(exc).__name__,detail=str(exc) if isinstance(exc,ValueError) else None)
                    self.wake.wait(15)
        except Exception as exc:self.status(state='STARTUP_BLOCKED',error=type(exc).__name__,detail=str(exc) if isinstance(exc,ValueError) else None)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
