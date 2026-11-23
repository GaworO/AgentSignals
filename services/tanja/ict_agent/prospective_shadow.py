"""Passive adapter: the existing Observer + evidence builder, never an executor.

An external, independently verified feed receipt is required. This module does
not manufacture entitlement. Historical catch-up is not a prospective forecast.
"""
import fcntl
import hashlib
import json
import time
from pathlib import Path
from .common import digest, encode
from .observer import Observer
from .market import context, validate_bar
from .ai import packet as evidence_packet
from .shadow_chart import build_chart, followup_due
from .shadow_sessions import scoped_rules, session_at, correlation
from .shadow_mvp import initialize, append, assessment

VERSION='passive-shadow-receipt-v2'


def gate(report, now):
    reasons=[]
    classification=report.get('classification')
    passive=report.get('passive_unverified_authorized') is True and classification in ('FEED_UNVERIFIED','DELAYED')
    if classification!='VERIFIED_REALTIME' and not passive:reasons.append('FEED_NOT_VERIFIED_REALTIME')
    required=('instrument_binding','exchange_calendar') if passive else ('entitlement','instrument_binding','exchange_calendar')
    for name in required:
        proof=report.get(name,{})
        if not (proof.get('verified') is True and proof.get('valid_from',float('inf'))<=now<proof.get('valid_until',0)):
            reasons.append(name.upper()+'_EVIDENCE_REQUIRED_OR_EXPIRED');continue
        path=Path(proof.get('evidence_file',''))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=proof.get('sha256'):
            reasons.append(name.upper()+'_EVIDENCE_HASH_MISMATCH')
    if set(report.get('symbols',[]))!={'NQ','MNQ','ES'}:reasons.append('THREE_INSTRUMENT_BINDINGS_REQUIRED')
    if set(report.get('instrument_binding',{}).get('tickers',{}))!={'NQ','MNQ','ES'}:reasons.append('EXACT_TICKER_BINDINGS_REQUIRED')
    if not report.get('exchange_calendar',{}).get('open_intervals_utc'):reasons.append('EXCHANGE_OPEN_INTERVALS_REQUIRED')
    return {'ready':not reasons,'reasons':reasons,'classification':report.get('classification','UNVERIFIED')}


def put(db,kind,key,value):
    raw=encode(value);ident=digest([kind,key,value])
    old=db.execute('SELECT payload FROM passive_log WHERE kind=? AND key=?',(kind,key)).fetchone()
    if old and old[0]!=raw:raise ValueError('PASSIVE_IMMUTABILITY_CONFLICT')
    db.execute('INSERT OR IGNORE INTO passive_log VALUES(?,?,?,?)',(ident,kind,key,raw))
    return ident


class PassiveObserver:
    """One writer. Commit the current decision and its receipt before another bar.

    mode OFFLINE_ADAPTER_TEST exists only in a test manifest; never LIVE. No
    credentials or order/provider transports are accepted by this class.
    """
    def __init__(self,store,knowledge,feed_report,*,enabled=False,now_ns=time.time_ns):
        self.store=store;self.knowledge=knowledge;self.now_ns=now_ns;self.report=feed_report
        with store.readonly() as db:self.manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
        self.testing=self.manifest.get('mode')=='OFFLINE_ADAPTER_TEST'
        if not self.testing:
            if not enabled:raise ValueError('PASSIVE_OBSERVATION_NOT_ENABLED')
            g=gate(feed_report,self.now_ns()/1e9)
            if not g['ready']:raise ValueError('|'.join(g['reasons']))
            if self.manifest.get('feed_report_sha256')!=digest(feed_report):raise ValueError('FEED_REPORT_MANIFEST_MISMATCH')
            if self.manifest.get('mode')!='PROSPECTIVE_SHADOW':raise ValueError('SEPARATE_PROSPECTIVE_JOURNAL_REQUIRED')
        if self.manifest.get('orders_enabled') is not False or self.manifest.get('paid_calls_enabled') is not False:
            raise ValueError('NO_ORDER_NO_PROVIDER_REQUIRED')
        self.lock=store.path.with_suffix('.passive.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:self.lock.close();raise
        try:
            initialize(store)
            with store.connect() as db:
                db.executescript('''CREATE TABLE IF NOT EXISTS passive_log(id TEXT PRIMARY KEY,kind TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(kind,key));
                CREATE TRIGGER IF NOT EXISTS passive_no_update BEFORE UPDATE ON passive_log BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
                CREATE TRIGGER IF NOT EXISTS passive_no_delete BEFORE DELETE ON passive_log BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;''')
            self.observer=Observer(store,knowledge,symbols=('NQ','MNQ','ES'))
            self.recover()
        except BaseException:self.close();raise

    def close(self):
        fcntl.flock(self.lock,fcntl.LOCK_UN);self.lock.close()

    def _write(self,kind,key,value):
        with self.store.connect() as db:
            db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE');put(db,kind,key,value)

    def recover(self):
        with self.store.readonly() as db:
            rows=db.execute("SELECT key,payload FROM passive_log a WHERE kind='INTENT' AND NOT EXISTS(SELECT 1 FROM passive_log b WHERE b.kind='DURABLE' AND b.key=a.key) ORDER BY json_extract(payload,'$.prepared_at_ns')").fetchall()
        for key,raw in rows:self._complete(key,json.loads(raw),recovered=True)

    def push(self,bars,source_receipts,*,transport_seen_at_ns=None):
        now=self.now_ns()
        if not self.testing:
            g=gate(self.report,now/1e9)
            if not g['ready']:raise ValueError('|'.join(g['reasons']))
        for b in bars:validate_bar(b)
        bars=sorted(bars,key=lambda b:b['symbol'])
        if len(bars)!=3 or {b['symbol'] for b in bars}!={'NQ','MNQ','ES'} or len({b['ts'] for b in bars})!=1:
            raise ValueError('PAIRED_MINUTE_REQUIRED')
        if not self.testing and any(b['source']!='TANJA_READ_ONLY_RECEIPT_TIME' for b in bars):raise ValueError('ACTUAL_SOURCE_RECEIPTS_REQUIRED')
        if not self.testing:
            bindings=self.report['instrument_binding']['tickers']
            if any(b['contract']!=bindings[b['symbol']] for b in bars):raise ValueError('UNVERIFIED_INSTRUMENT_BINDING')
            if any(not any(lo<=b['ts'] and b['ts']+60<=hi for lo,hi in self.report['exchange_calendar']['open_intervals_utc']) for b in bars):
                raise ValueError('OUTSIDE_VERIFIED_EXCHANGE_SESSION')
        if set(source_receipts)!={'NQ','MNQ','ES'}:raise ValueError('EXACT_RECEIPTS_REQUIRED')
        import math
        for b in bars:
            receipt=source_receipts[b['symbol']]
            if type(receipt) not in (int,float) or not math.isfinite(receipt) or receipt<b['ts']+60 or receipt>now/1e9 or math.ceil(receipt)!=b['received_at']:
                raise ValueError('SOURCE_RECEIPT_INCONSISTENT')
        key=digest(['bars',[(b['symbol'],b['contract'],b['ts']) for b in sorted(bars,key=lambda b:b['symbol'])]])[:24]
        with self.store.readonly() as db:
            old=db.execute("SELECT payload FROM passive_log WHERE kind='INTENT' AND key=?",(key,)).fetchone()
            done=db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE' AND key=?",(key,)).fetchone()
            last_ts=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
            cursor=db.execute('SELECT max(as_of) FROM batches').fetchone()[0]
        if old:
            previous=json.loads(old[0])
            if digest(previous['bars'])!=digest(bars) or previous['source_receipts']!=source_receipts:raise ValueError('CONFLICTING_DUPLICATE_BATCH')
            if done:return dict(json.loads(done[0]),duplicate=True)
            return self._complete(key,previous,recovered=True)
        if (last_ts is not None and bars[0]['ts']<=last_ts) or (cursor is not None and max(b['received_at'] for b in bars)<=cursor):
            self._write('LATE_EXCLUDED',key,{'reason':'NO_RETROSPECTIVE_REWRITE','source_receipts':source_receipts,'observed_at_ns':now})
            raise ValueError('LATE_RECEIPT_NO_RETROSPECTIVE_REWRITE')
        source_first=min(source_receipts.values());source_last=max(source_receipts.values())
        seen=transport_seen_at_ns if transport_seen_at_ns is not None else now
        if not int(source_last*1e9)<=seen<=now:raise ValueError('TRANSPORT_TIME_INCONSISTENT')
        # A poll/backlog processed after the next M1 close is catch-up, even if
        # its source receipt was originally realtime. Never backdate the decision.
        active_after=self.manifest.get('prospective_after_bar_start',0)
        mode='OFFLINE_ADAPTER_TEST' if self.testing else ('PROSPECTIVE_SHADOW' if bars[0]['ts']>=active_after and now<(bars[0]['ts']+120)*10**9 else 'CATCHUP_REPLAY')
        item={'bars':bars,'source_receipts':source_receipts,'source_first_received':source_first,'source_last_received':source_last,
              'transport_seen_at_ns':seen,'prepared_at_ns':now,'as_of':max(b['received_at'] for b in bars),'mode':mode,'session':session_at(bars[0]['ts']+60)}
        self._write('INTENT',key,item)
        return self._complete(key,item)

    def _complete(self,key,item,recovered=False):
        self.observer.push(item['bars']) # idempotent if committed before a crash
        self._project(key,item)
        # This is an upper bound on the actual commit time, not a fabricated
        # exact SQLite commit timestamp. No later batch is ingested before it.
        observed=self.now_ns()
        timely=not recovered and item['mode']=='PROSPECTIVE_SHADOW' and observed<(item['bars'][0]['ts']+120)*10**9
        receipt={'batch_id':key,'snapshot_as_of':item['as_of'],'write_started_at_ns':item['prepared_at_ns'],
                 'commit_observed_at_ns':observed,'recovered_after_interruption':recovered,
                 'prospective_timing_verified':timely,'mode':item['mode'],
                 'effective_decision_mode':'PROSPECTIVE_SHADOW' if timely else 'OFFLINE_ADAPTER_TEST' if self.testing else 'CATCHUP_OR_UNPROVEN_TIMING',
                 'orders_sent':0,'llm_status':'PENDING_LLM'}
        self._write('DURABLE',key,receipt)
        return receipt

    def _project(self,key,item):
        at=item['as_of']
        with self.store.connect() as db:
            db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM shadow_checkpoints WHERE batch_id=?',(key,)).fetchone():return
            memory=json.loads(db.execute('SELECT payload FROM memory WHERE id=1').fetchone()[0])
            if memory['cursor']!=at:raise ValueError('UNPROJECTED_BATCH_ORDER')
            ctx=context(memory['markets'],at)
            events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM events WHERE as_of=?',(at,))]
            for (raw,) in db.execute('SELECT payload FROM setups'):
                s=json.loads(raw)
                oldrow=db.execute("SELECT payload FROM shadow_records WHERE candidate_id=? AND stage='ASSESSMENT' ORDER BY as_of DESC LIMIT 1",(s['id'],)).fetchone();old=json.loads(oldrow[0]) if oldrow else None
                signature=digest({k:s.get(k) for k in ('state','sequence','supporting','contradicting','missing','last_transition')})
                if old and old['signature']==signature and not followup_due(s,old,ctx,events,at):continue
                rid=memory['retrieval_ids'].get(s['id'])
                if not rid and old:
                    prior=json.loads(db.execute('SELECT payload FROM shadow_evidence WHERE id=?',(old['evidence_packet_id'],)).fetchone()[0]);rid=next(iter(prior['snapshot']['retrieval_ids']),None)
                snap={'id':digest([s['id'],at]),'as_of':at,'context':ctx,'events':[],'setups':[s],'retrieval_ids':[rid] if rid else [],'intermarket':{}}
                packet=evidence_packet(self.store,snap);packet['source_records']=scoped_rules(self.knowledge,packet['source_records'],s['symbol'])
                chart=build_chart(db,packet,s,ctx,at);packet['chart']=chart
                packet.update(mode=item['mode'],semantic_status='PENDING_LLM',source_qualified=False,execution_readiness='BLOCKED_RESEARCH_ONLY',persistence={'batch_id':key,'prepared_at_ns':item['prepared_at_ns'],'durability_receipt_table':'passive_log'})
                eid=digest(packet);db.execute('INSERT OR IGNORE INTO shadow_evidence VALUES(?,?)',(eid,encode(packet)))
                for o in chart['derived_observations']:db.execute('INSERT OR IGNORE INTO shadow_observations VALUES(?,?)',(o['id'],encode(o)))
                state,missing,hypothesis=assessment(s,ctx)
                common={'symbol':s['symbol'],'direction':s['direction'],'model':s['research_policy'],'source_qualified':False,'execution_eligible':False,'orders_enabled':False,'trade_parameters':None,
                        'semantic_status':'PENDING_LLM','origin':'DETERMINISTIC_OBSERVATION','observation_mode':item['mode'],'persistence_batch':key,'evidence_packet_id':eid,'evidence_ids':s['supporting']+s['contradicting'],
                        'source_rule_ids':[r['id'] for r in packet['source_records']],'session':session_at(s['created_at']),'observation_session':session_at(at),'chart_state':chart['chart_state'],'correlation':correlation(s)}
                if not old:
                    append(db,s['id'],at,'DETECTED',dict(common,root_event=s['root_event']))
                    append(db,s['id'],at,'CONTEXT_ASSESSED',dict(common,context_status='COMPUTED_RANGES_NOT_SELECTED_ICT_DEALING_RANGE'))
                    append(db,s['id'],at,'SOURCE_EVALUATED',dict(common,evaluations=[{'rule_id':r['id'],'status':'RELEVANT_ONLY','source_reference_verified':r['id'] in self.knowledge.verified,'market_applicability':'UNRESOLVED_REQUIRES_SEMANTIC_REVIEW'} for r in packet['source_records']]))
                append(db,s['id'],at,'ASSESSMENT',dict(common,decision=state,hypothesis_status=hypothesis,signature=signature,detector_state=s['state'],observed_sequence=s['sequence'],missing_requirements=missing,
                    next_confirmation=[x for x in ('DISPLACEMENT','STRUCTURE_BREAK','FVG') if x not in s['sequence']],invalidation={'condition':'LATER_M1_CLOSE_BEYOND_RECORDED_SWEEP_EXTREME','level':s['sweep_extreme'],'direction':s['direction'],'scope':'RESEARCH_REVERSAL_ONLY'}))
                if old:
                    durable=db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE' AND key=?",(old['persistence_batch'],)).fetchone();d=json.loads(durable[0]) if durable else {}
                    eligible=bool(d.get('prospective_timing_verified') and d['commit_observed_at_ns']<int(item['source_first_received']*1e9))
                    append(db,s['id'],at,'OUTCOME',dict(common,status='INVALIDATED_RESEARCH_GEOMETRY' if state=='REJECTED' else 'UNRESOLVED',forecast_as_of=old['as_of'],forecast_commit_observed_at_ns=d.get('commit_observed_at_ns'),outcome_first_source_receipt=item['source_first_received'],prospective_outcome_eligible=eligible,
                        added_confirmations=[x for x in s['sequence'] if x not in old['observed_sequence']],source_setup_confirmed=False,fill=None,pnl=None,reason=s.get('last_transition',{}).get('reason')))
            db.execute('INSERT INTO shadow_checkpoints VALUES(?,?,?)',(key,at,encode({'persistence_batch':key,'mode':item['mode']})))


def status(store):
    with store.readonly() as db:
        manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
        receipts=[json.loads(r[0]) for r in db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE'")]
        return {'mode':manifest['mode'],'durable_batches':len(receipts),'verified_prospective_batches':sum(r['prospective_timing_verified'] for r in receipts),
                'unreceipted_intents':db.execute("SELECT count(*) FROM passive_log a WHERE kind='INTENT' AND NOT EXISTS(SELECT 1 FROM passive_log b WHERE b.kind='DURABLE' AND b.key=a.key)").fetchone()[0],
                'llm_status':'PENDING_LLM','orders_enabled':False,'paid_calls_enabled':False}
