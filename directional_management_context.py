"""Closed-candle monitoring for owned Directional positions; no trading rules."""
import json
import sqlite3
from pathlib import Path
import directional_hybrid_context as hybrid
from directional_position_manager import digest, fresh


def packet_from_bars(api, contract, now_ms):
    """Use only candles both closed and received by decision time.

    UTC-aligned aggregates require every constituent M1 candle. Cross-market
    evidence stays UNKNOWN when the existing hybrid source is unavailable.
    """
    with api.connect() as db:
        rows=db.execute("SELECT ts_ms,bar_json FROM bars WHERE contract=? AND tf='M1' AND ts_ms+60000<=? AND received_ms<=? ORDER BY ts_ms DESC LIMIT 5760",
                        (contract,now_ms,now_ms)).fetchall()
    if not rows:raise ValueError('MANAGER_CLOSED_CANDLES_UNAVAILABLE')
    minutes={int(r[0])//1000:json.loads(r[1]) for r in rows}
    cutoff=max(minutes)+60
    if not 0<=now_ms/1000-cutoff<=120:raise ValueError('MANAGER_CLOSED_CANDLES_STALE')
    evidence={}
    for tf in (1,5,15,60,240):
        size=tf*60
        for start in sorted({t//size*size for t in minutes}):
            stamps=list(range(start,start+size,60))
            if start+size>cutoff or any(t not in minutes for t in stamps):continue
            bars=[minutes[t] for t in stamps]
            evidence[f'MNQ:{tf}:{start}']=dict(kind='bar',symbol='MNQ',contract=contract,timeframe=tf,time=start,
                available_at=start+size,open=bars[0]['open'],high=max(b['high'] for b in bars),
                low=min(b['low'] for b in bars),close=bars[-1]['close'],
                source='ab_v3.bars:closed_received_M1_UTC_complete_aggregation')
    limits={1:240,5:192,15:128,60:96,240:24}
    keep={k for tf,limit in limits.items() for k in sorted((k for k,e in evidence.items() if e['timeframe']==tf),key=lambda k:evidence[k]['time'])[-limit:]}
    evidence={k:e for k,e in evidence.items() if k in keep}
    return dict(owner='AB_DIRECTIONAL',as_of=cutoff,evidence=evidence,source_packet_id=digest(evidence),
                coverage=dict(retained_closed_bars_per_tf={str(k):v for k,v in limits.items()},missing_or_older_history='UNKNOWN'))


def monitor(manager, api, oid, now_ms, *, shared_path=None, quote=None):
    s=manager.get(oid)
    if not s or not s.get('remaining_quantity'):return s
    try:
        packet=packet_from_bars(api,s['contract'],now_ms)
        # Reuse causally current ES/NQ context; never substitute NQ prices for MNQ.
        if shared_path and Path(shared_path).is_file():
            with sqlite3.connect('file:'+str(shared_path)+'?mode=ro',uri=True) as db:
                row=db.execute('SELECT packet,observed FROM hybrid_market WHERE id=1').fetchone()
            if row and 0<=now_ms/1000-row[1]<=120:
                other=json.loads(row[0])
                if 0<=packet['as_of']-other['as_of']<=120:
                    for k,e in other['evidence'].items():
                        if e.get('symbol') in ('ES','NQ') and e.get('kind')=='bar' and e.get('available_at',float('inf'))<=packet['as_of']:
                            packet['evidence']['shared:'+k]=dict(e,evidence_ids=[])
        packet['source_packet_id']=digest(packet['evidence'])
        previous=s.get('market_monitor',{})
        if previous.get('packet',{}).get('source_packet_id')==packet['source_packet_id'] and previous.get('broker_fingerprint')==s['broker_fingerprint']:
            return s
        original=s['frozen']['order'];setup=dict(original,activation_ms=original.get('activation_ms',0),expiry_ms=original.get('expiry_ms',0))
        context=hybrid.build(packet,setup)
        z=1 if s['direction']=='LONG' else -1
        closed=[e for e in packet['evidence'].values() if e['symbol']=='MNQ' and e['timeframe']==1]
        last=max(closed,key=lambda e:e['available_at'])['close']
        mark=dict(basis='CLOSED_M1_OBSERVATION_NOT_EXECUTABLE',price=last,as_of_ms=packet['as_of']*1000,
                  r=z*(last-s['average_entry'])/s['initial_risk_points'],remaining_target_points=z*(s['last_target']-last))
        if quote and quote.get('contract')==s['contract'] and quote.get('execution_eligible') is True and quote.get('timestamp_semantics_verified') is True and fresh(now_ms,quote.get('as_of_ms'),quote.get('max_age_ms',0)):
            px=quote['bid'] if z==1 else quote['ask']
            mark=dict(basis='VERIFIED_EXECUTABLE_QUOTE',price=px,as_of_ms=quote['as_of_ms'],r=z*(px-s['average_entry'])/s['initial_risk_points'],remaining_target_points=z*(s['last_target']-px))
        record=dict(status='OBSERVATIONAL',as_of_ms=packet['as_of']*1000,observed_ms=now_ms,
                    packet=packet,context=context,position_mark=mark,broker_fingerprint=s['broker_fingerprint'],authority='NO_ACTION_PERMISSION')
    except (ValueError,KeyError,TypeError,sqlite3.Error,OSError) as exc:
        record=dict(status='UNKNOWN',observed_ms=now_ms,reason=str(exc),authority='NO_ACTION_PERMISSION')
    with manager.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=manager.get(oid)
        if current['broker_fingerprint']!=s['broker_fingerprint']:return current
        current['market_monitor']=record
        key=digest(dict(kind='MARKET_MONITOR',packet_id=record.get('packet',{}).get('source_packet_id'),
                        fingerprint=s['broker_fingerprint'],reason=record.get('reason')))
        db.execute('INSERT OR IGNORE INTO ict_position_events VALUES(?,?,?,?)',(manager.binding,oid,key,json.dumps(record)))
        manager.save(db,current)
    return current


# A single observation task per process: slow context calculation cannot block
# native protection recovery, Guard, or the original M3 exit dispatch.
import threading
_MONITOR_LOCK=threading.Lock()

def schedule(transport, oid):
    if not _MONITOR_LOCK.acquire(blocking=False):return False
    def work():
        try:
            from directional_position_manager import Manager
            manager=Manager(transport.api.connect,transport.binding)
            shared=Path(transport.sync.config.get('DATA_DIR','.'))/'ab_ai_context.sqlite3'
            monitor(manager,transport.api,oid,int(transport.clock()*1000),shared_path=shared)
        except Exception:
            # Next broker poll retries observation; no mutation or stale advice
            # obtains action permission when monitoring is unavailable.
            pass
        finally:_MONITOR_LOCK.release()
    threading.Thread(target=work,daemon=True,name='directional-position-context').start()
    return True
