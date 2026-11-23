"""Bounded LOCAL observer. One temporary SSH SQL reader; no Railway installation.

The remote subprocess only opens the intake SQLite in query_only mode. Local
mirror + envelopes preserve exact receiver timestamps. No provider/broker code.
"""
import argparse, hashlib, json, math, os, selectors, shlex, sqlite3, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.run_passive_shadow import disable_external_transports
from services.tanja.ict_agent.prospective_shadow import PassiveObserver,gate,status
from services.tanja.ict_agent.bridge import read_tanja
from services.tanja.ict_agent.common import digest,encode
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.knowledge import Knowledge

REMOTE='''import sqlite3,json,time,sys
after=int(sys.argv[1]);deadline=float(sys.argv[2])
while time.time()<deadline:
 now=time.time()
 with sqlite3.connect('file:/data/builder50/market_context/tanja.sqlite3?mode=ro',uri=True,timeout=3) as db:
  db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  bindings={}
  for key,raw in db.execute("SELECT key,value FROM metadata WHERE key IN ('tickers','collection_tickers')"):
   bindings.update({k:v for k,v in json.loads(raw).items() if k in ('NQ','MNQ','ES')})
  rows=[];ends=[]
  for symbol,table in [('NQ','collection_bars'),('MNQ','bars'),('ES','bars')]:
   part=db.execute('SELECT symbol,start,end,received,payload FROM '+table+' WHERE symbol=? AND start>? AND received<=? AND end<=? ORDER BY start LIMIT 5000',(symbol,after,now,now)).fetchall()
   rows.extend(part)
   if part:ends.append(max(r[1] for r in part))
 print(json.dumps({'captured_at':now,'read_only':True,'bindings':bindings,'rows':rows},separators=(',',':')),flush=True)
 if len(ends)==3:after=max(after,min(ends)-900)
 time.sleep(10)
'''

def ssh_command(after,deadline):
    return ['/opt/homebrew/bin/railway','ssh','--project','26df4462-061e-48e5-ad8a-b364909a1b8f','--service','5311ba22-8e59-4197-a1a5-490325e91911','--environment','production','python -u -c '+shlex.quote(REMOTE)+' '+str(int(after))+' '+str(float(deadline))]

def immutable(path,value):
    raw=encode(value)
    if path.exists():
        if path.read_text()!=raw:raise ValueError('IMMUTABLE_FILE_CONFLICT')
    else:
        with path.open('x') as f:f.write(raw);f.flush();os.fsync(f.fileno())

def mirror_ingest(path,envelope,seen_ns,bindings):
    if envelope.get('read_only') is not True or envelope['bindings']!=bindings:raise ValueError('SOURCE_BINDING_CHANGED')
    if not math.isfinite(envelope['captured_at']) or envelope['captured_at']>seen_ns/1e9+1:raise ValueError('REMOTE_CLOCK_INCONSISTENT')
    added=0
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        for table in ('bars','collection_bars'):
            db.execute('CREATE TABLE IF NOT EXISTS '+table+'(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT,PRIMARY KEY(symbol,start))')
            for action in ('UPDATE','DELETE'):
                db.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_'+action+' BEFORE '+action+' ON '+table+" BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END")
        for row in envelope['rows']:
            symbol,start,end,received,raw=row;p=json.loads(raw)
            if symbol not in bindings or p.get('ticker')!=bindings[symbol] or p.get('symbol')!=symbol or end!=start+60 or not end<=received<=envelope['captured_at']:raise ValueError('SOURCE_ROW_INVALID')
            if p.get('bar_open_ms')!=start*1000 or p.get('bar_close_ms')!=end*1000 or p.get('confirmed') is not True or p.get('standard') is not True:raise ValueError('SOURCE_BAR_INVALID')
            table='collection_bars' if symbol=='NQ' else 'bars'
            old=db.execute('SELECT symbol,start,end,received,payload FROM '+table+' WHERE symbol=? AND start=?',(symbol,start)).fetchone()
            if old and list(old)!=row:raise ValueError('SOURCE_REVISION_REQUIRES_REVIEW')
            if not old:db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?)',row);added+=1
    return added

def process_available(obs,mirror,seen_ns):
    with obs.store.readonly() as db:last=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
    batches,gaps=read_tanja(mirror,after_start=last if last is not None else -1,frozen_at=seen_ns/1e9)
    now=seen_ns/1e9
    pending=[g['ts'] for g in gaps if g['ts']+60+180>now]
    cutoff=min(pending,default=2**62)
    complete={b[0]['ts'] for b in batches}
    first=last+60 if last is not None else min(complete,default=None)
    if first is not None:
        intervals=obs.report['exchange_calendar']['open_intervals_utc']
        for t in range(first,int(now//60)*60-240,60):
            if t not in complete and any(lo<=t and t+60<=hi for lo,hi in intervals):
                obs._write('SOURCE_GAP',str(t),{'ts':t,'missing':next((g['missing'] for g in gaps if g['ts']==t),['NQ','MNQ','ES']),'policy':'NO_SYNTHETIC_BARS_NO_RETROSPECTIVE_REWRITE','settled_after_grace_seconds':180})
    done=0
    with sqlite3.connect(mirror.as_uri()+'?mode=ro',uri=True) as db:
        for bars in batches:
            if bars[0]['ts']>=cutoff:break
            receipts={b['symbol']:db.execute('SELECT received FROM '+('collection_bars' if b['symbol']=='NQ' else 'bars')+' WHERE symbol=? AND start=?',(b['symbol'],b['ts'])).fetchone()[0] for b in bars}
            obs.push(bars,receipts,transport_seen_at_ns=seen_ns);done+=1
    return done,gaps

def snapshot_status(obs,mirror,phase,deadline):
    out=status(obs.store);out.update(operational_state=phase,feed_classification=obs.report['classification'],heartbeat_at=time.time(),stop_at=deadline,pid=os.getpid(),broker_transport='ABSENT',source_qualified=0)
    with obs.store.readonly() as db:
        receipts={k:json.loads(p) for k,p in db.execute("SELECT key,payload FROM passive_log WHERE kind='DURABLE'")}
        detected=[json.loads(r[0]) for r in db.execute("SELECT payload FROM shadow_records WHERE stage='DETECTED'")]
        genuine={r['candidate_id'] for r in detected if receipts.get(r['persistence_batch'],{}).get('prospective_timing_verified')}
        allrows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM shadow_records ORDER BY as_of')]
        assessments={r['candidate_id']:r for r in allrows if r['stage']=='ASSESSMENT' and r['candidate_id'] in genuine}
        out.update(prospective_candidates=len(genuine),catchup_candidates=len(detected)-len(genuine),candidate_counts={s:sum(r['symbol']==s for r in assessments.values()) for s in ('NQ','MNQ','ES')},decisions={s:sum(r['decision']==s for r in assessments.values()) for s in ('WAIT','REJECTED','DATA_INSUFFICIENT')},prospective_outcomes=sum(r['stage']=='OUTCOME' and r['candidate_id'] in genuine and r.get('prospective_outcome_eligible',False) for r in allrows),source_gaps=db.execute("SELECT count(*) FROM passive_log WHERE kind='SOURCE_GAP'").fetchone()[0],last_bar_start=db.execute('SELECT max(ts) FROM bars').fetchone()[0],candidate_ids=sorted(genuine))
    with sqlite3.connect(mirror.as_uri()+'?mode=ro',uri=True) as db:
        out['feeds']={}
        for s in ('NQ','MNQ','ES'):
            table='collection_bars' if s=='NQ' else 'bars'
            row=db.execute('SELECT end,received FROM '+table+' WHERE symbol=? ORDER BY start DESC LIMIT 1',(s,)).fetchone()
            out['feeds'][s]={'latest_close':row[0],'latest_received':row[1],'close_to_receipt_seconds':row[1]-row[0],'age_seconds':time.time()-row[0]} if row else {}
    return out

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--inputs',required=True);p.add_argument('--feed-report',required=True);p.add_argument('--stop-at',type=float,required=True);p.add_argument('--start-at',type=int,required=True)
    a=p.parse_args();disable_external_transports();out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True);(out/'envelopes').mkdir(exist_ok=True)
    report=json.loads(Path(a.feed_report).read_text())
    if not gate(report,time.time())['ready'] or not time.time()<a.stop_at<=report['exchange_calendar']['valid_until']:raise ValueError('PREFLIGHT_FAILED')
    components=['services/tanja/ict_agent/'+n for n in ('prospective_shadow.py','shadow_mvp.py','shadow_chart.py','shadow_sessions.py','observer.py','scenarios.py','bridge.py')]+['tools/observe_railway_shadow.py','tools/run_passive_shadow.py']
    hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in components}
    mp=out/'manifest.json'
    if mp.exists():
        manifest=json.loads(mp.read_text())
        if manifest['runtime_sha256']!=hashes or manifest['feed_report_sha256']!=digest(report) or manifest['end']!=a.stop_at:raise ValueError('FROZEN_RUNTIME_CHANGED')
    else:
        manifest={'version':'railway-passive-local-v1','mode':'PROSPECTIVE_SHADOW','session':'continuous','start':a.start_at,'end':a.stop_at,'coverage':{},'symbols':['NQ','MNQ','ES'],'feed_report_sha256':digest(report),'runtime_sha256':hashes,'created_at_ns':time.time_ns(),'prospective_after_bar_start':int(time.time()//60)*60+60,'orders_enabled':False,'paid_calls_enabled':False,'knowledge_recovery_sha256':hashlib.sha256(Path(a.inputs,'recovery.json').read_bytes()).hexdigest()};immutable(mp,manifest)
    store=Store(out/'observer.sqlite3',manifest);knowledge=Knowledge(corpus=Path(a.inputs)/'knowledge/corpus',atlas=Path(a.inputs)/'knowledge/atlas')
    obs=PassiveObserver(store,knowledge,report,enabled=True);mirror=out/'intake-mirror.sqlite3';proc=None;phase='BLOCKED'
    try:
        with store.readonly() as db:last=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
        obs._write('PROCESS_START',str(time.time_ns()),{'started_at_ns':time.time_ns(),'recovered_cursor':last,'runtime_sha256':hashes,'stop_at':a.stop_at})
        proc=subprocess.Popen(ssh_command(max(a.start_at-1,(last-900) if last else a.start_at-1),a.stop_at),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        for line in proc.stdout:
            seen=time.time_ns()
            if time.time()>=a.stop_at:phase='COMPLETED_BOUNDED_RUN';break
            if not line.startswith('{'):continue
            envelope=json.loads(line);immutable(out/'envelopes'/(digest(envelope)+'.json'),dict(envelope,local_transport_seen_at_ns=seen))
            added=mirror_ingest(mirror,envelope,seen,report['instrument_binding']['tickers'])
            processed,gaps=process_available(obs,mirror,seen)
            phase='PROSPECTIVE_SHADOW_RUNNING'
            state=snapshot_status(obs,mirror,phase,a.stop_at);state.update(new_mirror_rows=added,processed_this_poll=processed,pending_pairs=gaps)
            temp=out/'status.tmp';temp.write_text(json.dumps(state,indent=2));temp.replace(out/'status.json')
            print(json.dumps({k:state[k] for k in ('heartbeat_at','operational_state','processed_this_poll','prospective_candidates','prospective_outcomes','source_gaps','last_bar_start')}),flush=True)
        else:
            phase='COMPLETED_BOUNDED_RUN' if time.time()>=a.stop_at else 'BLOCKED_INTAKE_DISCONNECTED'
    except BaseException as e:
        phase='STOPPED_BY_OPERATOR' if isinstance(e,KeyboardInterrupt) else 'BLOCKED_'+type(e).__name__.upper()
        obs._write('PROCESS_STOP',str(time.time_ns()),{'at':time.time(),'status':phase,'error_type':type(e).__name__})
        raise
    finally:
        if proc and proc.poll() is None:proc.terminate()
        if mirror.exists():
            state=snapshot_status(obs,mirror,phase,a.stop_at);(out/'status.json').write_text(json.dumps(state,indent=2))
        obs.close()

if __name__=='__main__':main()
