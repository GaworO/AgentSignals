"""Preflight by default. Explicit passive enable + verified feed required.

Read only an existing local/mounted intake DB using the committed bridge. It
never starts the intake application, connects to a broker or calls an LLM.
"""
import argparse,json,os,sys,time,socket,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
def deny(*args,**kwargs):raise OSError('PASSIVE_OBSERVER_NETWORK_DISABLED')
def disable_external_transports():
    for key in list(os.environ):
        if any(x in key for x in ('TOKEN','PASSWORD','SECRET','API_KEY','WEBHOOK')):os.environ.pop(key)
    socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
from services.tanja.ict_agent.prospective_shadow import PassiveObserver,gate,put,status,VERSION
from services.tanja.ict_agent.bridge import read_tanja
from services.tanja.ict_agent.common import digest
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.knowledge import Knowledge


def poll_once(observer,source,now):
    import sqlite3
    with observer.store.readonly() as db:last=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
    # The bridge preserves server receipt availability. Unpaired/late bars are
    # recorded, never filled in or allowed to rewrite an earlier decision.
    batches,gaps=read_tanja(source,after_start=last if last is not None else -1,frozen_at=now,limit=15000)
    seen=time.time_ns()
    # read_tanja reports unpaired minutes. Also retain gaps where all symbols
    # are absent, within the independently verified exchange-open intervals.
    # One-minute grace distinguishes missing settled bars from alert latency.
    first=last+60 if last is not None else min((b[0]['ts'] for b in batches),default=None)
    if first is not None:
        seen_minutes={b[0]['ts'] for b in batches}|{g['ts'] for g in gaps}
        stop=min(int(now//60)*60-60,first+15000*60)
        intervals=observer.report.get('exchange_calendar',{}).get('open_intervals_utc',[])
        for t in range(first,stop,60):
            if t not in seen_minutes and any(lo<=t and t+60<=hi for lo,hi in intervals):
                gaps.append({'ts':t,'missing':['ES','MNQ','NQ']})
    if gaps:
        observer._write('SOURCE_GAPS',digest([gaps,now]),{'missing_pairs':gaps,'received_before':now})
    processed=0
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        health={}
        for symbol in ('NQ','MNQ','ES'):
            table='collection_bars' if symbol=='NQ' else 'bars'
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            row=db.execute('SELECT end,received FROM '+table+' WHERE symbol=? AND received<=? AND end<=? ORDER BY start DESC LIMIT 1',(symbol,now,now)).fetchone() if exists else None
            health[symbol]={'latest_close':row[0] if row else None,'latest_source_receipt':row[1] if row else None,
                            'close_age_seconds':now-row[0] if row else None,
                            'status':'UNAVAILABLE' if not row else 'STALE_OR_CLOSED' if now-row[0]>180 else 'RECENT_RECEIPT_NOT_ENTITLEMENT_PROOF'}
        observer._write('FEED_POLL',digest([now,health]),{'checked_at':now,'source_read_only':True,'instruments':health})
        for bars in batches:
            receipts={b['symbol']:db.execute('SELECT received FROM '+('collection_bars' if b['symbol']=='NQ' else 'bars')+' WHERE symbol=? AND start=?',(b['symbol'],b['ts'])).fetchone()[0] for b in bars}
            observer.push(bars,receipts,transport_seen_at_ns=seen);processed+=1
    return {'processed':processed,'missing_pairs':gaps,'feed_health':health}


def main():
    disable_external_transports()
    p=argparse.ArgumentParser();p.add_argument('--feed-report',required=True);p.add_argument('--enable-passive',action='store_true')
    p.add_argument('--source-db');p.add_argument('--inputs');p.add_argument('--output');p.add_argument('--polls',type=int,default=1);p.add_argument('--interval',type=int,default=10)
    a=p.parse_args();report=json.loads(Path(a.feed_report).read_text());g=gate(report,time.time())
    print(json.dumps({'gate':g,'enabled':a.enable_passive,'orders_enabled':False,'llm_status':'PENDING_LLM'}),flush=True)
    if not g['ready'] or not a.enable_passive:return 2 if a.enable_passive else 0
    if not a.source_db or not a.inputs or not a.output or not 1<=a.polls<=8640 or not 1<=a.interval<=60:p.error('Source DB, verified inputs, output and bounded polling required')
    from services.tanja.ict_agent.shadow_mvp import inputs_check
    inputs_check(a.inputs);source=Path(a.source_db).resolve();out=Path(a.output).resolve()
    if not source.is_file():raise ValueError('READ_ONLY_SOURCE_DB_UNAVAILABLE')
    if source==out/'observer.sqlite3':raise ValueError('SOURCE_CANNOT_BE_DESTINATION')
    components=('prospective_shadow.py','shadow_mvp.py','shadow_chart.py','shadow_sessions.py','observer.py','scenarios.py','bridge.py')
    manifest={'version':VERSION,'mode':'PROSPECTIVE_SHADOW','session':'continuous','start':0,'end':2**62,'coverage':{},'symbols':['NQ','MNQ','ES'],
        'feed_report_sha256':digest(report),'source_path_sha256':hashlib.sha256(str(source).encode()).hexdigest(),'knowledge_recovery_sha256':hashlib.sha256(Path(a.inputs,'recovery.json').read_bytes()).hexdigest(),
        'runtime_sha256':{n:hashlib.sha256((ROOT/'services/tanja/ict_agent'/n).read_bytes()).hexdigest() for n in components},'orders_enabled':False,'paid_calls_enabled':False}
    store=Store(out/'observer.sqlite3',manifest);knowledge=Knowledge(corpus=Path(a.inputs)/'knowledge/corpus',atlas=Path(a.inputs)/'knowledge/atlas')
    observer=PassiveObserver(store,knowledge,report,enabled=True)
    try:
        for i in range(a.polls):
            print(json.dumps(poll_once(observer,source,time.time())),flush=True)
            if i+1<a.polls:time.sleep(a.interval)
        print(json.dumps(status(store)),flush=True)
    finally:observer.close()
    return 0

if __name__=='__main__':raise SystemExit(main())
