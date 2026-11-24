"""Read-only connection to existing Tanja intake. Does not import/start its app."""
import json
import math
import sqlite3
from pathlib import Path
from collections import defaultdict
from .market import validate_bar,bar_id
from .common import encode

def read_tanja(path,*,after_start=-1,frozen_at,limit=15000,before_start=2**62):
    if type(limit) is not int or not 1<=limit<=100000:raise ValueError('READ_LIMIT')
    grouped=defaultdict(list)
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        for table,symbols in (('bars',('ES','MNQ')),('collection_bars',('NQ',))):
            exists=db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone()
            if not exists:continue
            for symbol in symbols:
                rows=db.execute(f'SELECT start,received,payload FROM {table} WHERE symbol=? AND start>? AND start<? AND end<=? AND received<=? ORDER BY start LIMIT ?',
                    (symbol,after_start,before_start,frozen_at,frozen_at,limit)).fetchall()
                for start,received,payload in rows:
                    p=json.loads(payload)
                    if p.get('symbol')!=symbol or p.get('confirmed') is not True or p.get('standard') is not True or p.get('bar_open_ms')!=start*1000 or p.get('bar_close_ms')!=(start+60)*1000:
                        raise ValueError('INVALID_TANJA_SOURCE_BAR')
                    b=dict(symbol=symbol,contract=p['ticker'],ts=start,received_at=max(start+60,math.ceil(received)),
                           source='TANJA_READ_ONLY_RECEIPT_TIME',**{k:p[k] for k in ('open','high','low','close')})
                    grouped[start].append(validate_bar(b))
    # Intake is per instrument. Missing peers are diagnostics for assessment,
    # never a reason to discard an actually received candle from another market.
    batches=[];gaps=[]
    for ts,rows in sorted(grouped.items()):
        batches.append(rows)
        missing=sorted({'ES','NQ','MNQ'}-{b['symbol'] for b in rows})
        if missing:gaps.append(dict(ts=ts,missing=missing))
    batches.sort(key=lambda rows:(max(b['received_at'] for b in rows),rows[0]['ts']))
    return batches,gaps

def observe_once(observer,source_path,now,*,max_age_seconds=300,max_batches=5):
    # Live observation only. Historical research uses read_tanja explicitly.
    # Bound the time window on EVERY poll/restart, including an empty journal.
    if type(max_age_seconds) is not int or not 60<=max_age_seconds<=3600:raise ValueError('LIVE_CATCHUP_AGE_LIMIT')
    if type(max_batches) is not int or not 1<=max_batches<=60:raise ValueError('LIVE_CATCHUP_BATCH_LIMIT')
    # Opening a WAL reader may update shared-memory bookkeeping too.
    if getattr(observer.store,'write_guard',None):observer.store.write_guard()
    with observer.store.readonly() as db:
        latest=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
        # Re-read the bounded window: a later receipt for one symbol must not
        # disappear behind the global max(ts) of a faster symbol.
        known={i:p for i,p in db.execute('SELECT id,payload FROM bars WHERE ts>=?',
                                         (int(now//60)*60-max_age_seconds,))}
    floor=int(now//60)*60-max_age_seconds-1
    after=floor
    # Read the whole bounded window, then cap new writes. Limiting the SQL read
    # to max_batches would repeatedly return already-seen rows and starve peers.
    batches,gaps=read_tanja(source_path,after_start=after,frozen_at=now,
                           limit=(max_age_seconds+59)//60+1)
    processed=0;late=[]
    for rows in batches:
        unseen=[]
        for b in rows:
            prior=known.get(bar_id(b))
            if prior is not None:
                if prior!=encode(b):raise ValueError('BAR_IMMUTABILITY_CONFLICT')
            else:unseen.append(b)
        if not unseen:continue
        if processed>=max_batches:break
        result=observer.push(unseen,force_snapshot=True,observed_at=math.ceil(now))
        late.extend(result.get('late_bars',[]));processed+=1
    return dict(processed=processed,missing_pairs=gaps,late=late,source_read_only=True,orders_sent=0,
                intake_after_start=after,previous_latest_start=latest,
                history_not_replayed_before=floor+1 if latest is None or latest<floor else None)
