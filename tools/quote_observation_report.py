"""Read-only quote calibration. Measurements never certify a feed or approve policy.

Accepts saved QuoteClient evidence; groups closed wall-clock NYAM/NYPM windows.
No candles, model requests, broker writes or guessed quote timestamps.
"""
from collections import Counter
from datetime import datetime
import math
import statistics
from zoneinfo import ZoneInfo


def finite(x):
    return type(x) in (int,float) and math.isfinite(x)


def stats(values):
    values=sorted(x for x in values if finite(x))
    if not values:return {'n':0}
    return dict(n=len(values),min=values[0],median=statistics.median(values),
                p95=values[max(0,math.ceil(.95*len(values))-1)],max=values[-1])


def session(ms):
    dt=datetime.fromtimestamp(ms/1000,ZoneInfo('America/New_York'))
    minute=dt.hour*60+dt.minute
    name=('NYAM' if 570<=minute<660 else 'NYPM' if 810<=minute<960 else 'OUTSIDE') if dt.weekday()<5 else 'OUTSIDE'
    return dt.date().isoformat()+' '+name


def summarize(rows):
    from directional_market_data import quote_timestamps
    rows=sorted(rows,key=lambda r:r['observed_ms'])
    groups={}
    for r in rows:groups.setdefault(session(r['observed_ms']),[]).append(r)
    def group(items):
        spreads=[];ages=[];timestamp_errors=[];switches=[];off_tick=0;previous=None;gate_pass=0
        for r in items:
            raw=r.get('raw') or {};now=r['observed_ms']
            if raw:
                if previous and previous!=raw.get('dataSource'):
                    switches.append(dict(at_ms=now,previous=previous,current=raw.get('dataSource')))
                previous=raw.get('dataSource')
            bid,ask=raw.get('bid'),raw.get('ask')
            if finite(bid) and finite(ask) and 0<bid<=ask:
                spread=(ask-bid)/.25;spreads.append(spread)
                if any(abs(p/.25-round(p/.25))>1e-8 for p in (bid,ask)):off_tick+=1
            try:ages.append(now-min(quote_timestamps(raw).values()))
            except (ValueError,TypeError,OverflowError) as exc:timestamp_errors.append(type(exc).__name__)
            q=r.get('quote') or {}
            if q and 0<=q['quote_age_ms']<=1000 and q['spread_ticks']<=2:gate_pass+=1
        return dict(count=len(items),first_ms=items[0]['observed_ms'],last_ms=items[-1]['observed_ms'],
            sources=dict(Counter((r.get('quote') or {}).get('source','UNKNOWN') for r in items)),
            reasons=dict(Counter(r.get('reason') or 'NONE' for r in items)),
            spread_ticks_observed=stats(spreads),off_tick_count=off_tick,
            request_latency_ms=stats([r.get('request_latency_ms') for r in items]),
            reported_quote_age_ms=stats(ages),unsupported_timestamp_count=len(timestamp_errors),
            source_switches=switches,verified_executable_count=sum(bool((r.get('quote') or {}).get('execution_eligible')) for r in items),
            hypothetical_1000ms_2ticks_pass=gate_pass)
    return dict(groups={k:group(v) for k,v in groups.items()},
                timestamp_units='DOCUMENTED_REST_EPOCH_SECONDS_OR_UTC_DATETIME',
                statistically_validated_execution_parameters=False)


def review_movements(requests,reviews,quotes,max_alignment_ms=1000):
    """Exact saved review times + observed quotes only; gaps remain UNKNOWN."""
    out=[]
    for request in requests:
        review=next((r for r in reviews if r.get('id')==request.get('review_id')),None)
        # Schema-specific request/response timestamps must be supplied by caller.
        start,end=(review or {}).get('requested_at'),(review or {}).get('response_received_at')
        row=dict(order_id=request.get('order_id'),review_id=request.get('review_id'),status='UNKNOWN')
        if finite(start) and finite(end):
            row['latency_ms']=(end-start)*1000
            pair=[]
            for t in (start*1000,end*1000):
                candidates=[q for q in quotes if q.get('quote') and 0<=t-q['observed_ms']<=max_alignment_ms]
                pair.append(max(candidates,key=lambda q:q['observed_ms']) if candidates else None)
            if all(pair):
                row.update(status='RECORDED_OBSERVATIONS_NOT_EXECUTABLE',
                    before=pair[0],after=pair[1],ask_change_points=pair[1]['quote']['ask']-pair[0]['quote']['ask'],
                    bid_change_points=pair[1]['quote']['bid']-pair[0]['quote']['bid'])
            else:row['reason']='No quotes within alignment bound before both actual review timestamps; no interpolation.'
        else:row['reason']='Exact recorded request/response timing unavailable.'
        out.append(row)
    return out
