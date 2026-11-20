"""Read-only connection to existing Tanja intake. Does not import/start its app."""
import json
import math
import sqlite3
from pathlib import Path
from collections import defaultdict
from .market import validate_bar

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
    # Require all three for the existing paired-feed bridge. Missing pairs are
    # explicitly returned as gaps; no synthetic substitutes or permanent waits.
    batches=[];gaps=[]
    for ts,rows in sorted(grouped.items()):
        if {b['symbol'] for b in rows}=={'ES','NQ','MNQ'}:batches.append(rows)
        else:gaps.append(dict(ts=ts,missing=sorted({'ES','NQ','MNQ'}-{b['symbol'] for b in rows})))
    return batches,gaps

def observe_once(observer,source_path,now):
    with observer.store.readonly() as db:
        latest=db.execute('SELECT max(ts) FROM bars').fetchone()[0]
        row=db.execute('SELECT payload FROM memory WHERE id=1').fetchone()
        cursor=json.loads(row[0])['cursor'] if row else -1
    batches,gaps=read_tanja(source_path,after_start=latest if latest is not None else -1,frozen_at=now)
    processed=0;late=[]
    for rows in batches:
        available=max(b['received_at'] for b in rows)
        if available<=cursor:
            late.append(dict(ts=rows[0]['ts'],reason='RECEIPT_ORDER_CONFLICT_NO_RETROSPECTIVE_REWRITE'));continue
        observer.push(rows);cursor=available;processed+=1
    return dict(processed=processed,missing_pairs=gaps,late=late,source_read_only=True,orders_sent=0)
