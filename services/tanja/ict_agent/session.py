"""Bounded, external same-session replay writer; never starts an app or broker."""
import argparse,json,os,time,fcntl,sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .common import save,code_hash,digest
from .store import Store
from .observer import Observer
from .bridge import read_tanja
from .assets import verify
from .knowledge import Knowledge

NY=ZoneInfo('America/New_York')

def in_session(ts,day):
    local=datetime.fromtimestamp(ts,NY)
    return local.date().isoformat()==day and 570<=local.hour*60+local.minute<720

def build(source,target,release,day,frozen_at,progress=None):
    source=Path(source).resolve();target=Path(target).resolve();release=Path(release).resolve()
    if source==target:raise ValueError('SOURCE_MUST_REMAIN_READ_ONLY')
    if frozen_at>time.time():raise ValueError('FUTURE_CUTOFF_NOT_AVAILABLE')
    verify(release)
    knowledge=Knowledge(corpus=release/'corpus',atlas=release/'atlas')
    manifest=dict(version='ict-nyam-external-replay-v1',source=str(source),session=day,cutoff_policy='SEQUENTIAL_ACTUAL_RECEIPTS',
        code_hash=code_hash(),knowledge_version=knowledge.version,index_version=knowledge.index_version,
        mode='HISTORICAL_REPLAY_WITH_ACTUAL_RECEIPTS',orders_enabled=False)
    store=Store(target,manifest);observer=Observer(store,knowledge)
    with store.readonly() as db:
        latest=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
        row=db.execute('SELECT payload FROM memory WHERE id=1').fetchone()
    after=latest if latest is not None else -1
    cursor=json.loads(row[0])['cursor'] if row else -1
    report=dict(processed=0,gaps=[],late=[],frozen_at=frozen_at,source=str(source),orders_sent=0)
    while True:
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db:
            starts=[r[0] for r in db.execute('SELECT start FROM bars WHERE symbol IN (\'ES\',\'MNQ\') AND start>? AND end<=? AND received<=? UNION SELECT start FROM collection_bars WHERE symbol=\'NQ\' AND start>? AND end<=? AND received<=? ORDER BY start LIMIT 512', (after,frozen_at,frozen_at,after,frozen_at,frozen_at))]
        if not starts:break
        batches,gaps=read_tanja(source,after_start=after,before_start=starts[-1]+1,frozen_at=frozen_at,limit=512)
        report['gaps']+=gaps
        for bars in batches:
            ts=bars[0]['ts'];available=max(b['received_at'] for b in bars)
            if available<=cursor:
                report['late'].append(dict(ts=ts,reason='RECEIPT_ORDER_CONFLICT_NO_RETROSPECTIVE_REWRITE'))
            else:
                observer.push(bars,emit=in_session(ts,day));cursor=available;report['processed']+=1
            after=ts
        after=starts[-1]
        if progress:progress(dict(processed=report['processed'],last_start=after,last_as_of=cursor,gaps=len(report['gaps']),late=len(report['late'])))
    report.update(statistics=store.stats(integrity=True),knowledge_audit=knowledge.audit,
        limitations=['Continuous identifiers are not verified dated-contract mappings.','Missing pairs/receipt conflicts are not synthetic bars.',
        'Text-only source verification does not establish visual interpretation or profitable execution.'])
    save(target.parent/'session-build.json',report)
    return store,knowledge,report

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--target',required=True)
    p.add_argument('--release',required=True);p.add_argument('--date',required=True);p.add_argument('--frozen-at',type=int,required=True)
    a=p.parse_args()
    # One explicit offline writer; browser requests cannot invoke this command.
    target=Path(a.target);target.parent.mkdir(parents=True,exist_ok=True)
    with target.with_suffix('.writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        _,_,report=build(a.source,a.target,a.release,a.date,a.frozen_at,lambda x:print(json.dumps(x),flush=True))
        print(json.dumps({k:report[k] for k in ('processed','frozen_at','orders_sent','statistics')}),flush=True)

if __name__=='__main__':main()
