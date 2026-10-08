"""Geometry of explicitly selected market windows. Selection is not automated."""
from datetime import datetime, timedelta
from tanya_replay import ET


def window_catalog(markets, as_of):
    """Fixed, labelled reference choices, not retrospective optimal windows.

    Extrema and completeness are computed from closed M1 data. Windows ending
    'now' always end at the latest complete minute. Missing minutes invalidate
    a comparison, not just a single extreme. Windows are per ET calendar day.
    """
    day=datetime.fromtimestamp(as_of,ET).replace(hour=0,minute=0,second=0,microsecond=0)
    stamp=lambda h,m=0:int(day.replace(hour=h,minute=m).timestamp())
    now=as_of//60*60
    choices=[('overnight',stamp(0),stamp(9,30)),('preopen30',stamp(9),stamp(9,30)),
             ('rth_first15',stamp(9,30),stamp(9,45)),
             ('rth_to_now',stamp(9,30),now),('after_first15_to_now',stamp(9,45),now),
             ('day_to_now',stamp(0),now)]
    result={}
    for symbol,bars in markets.items():
        lookup={b.time:b for b in bars if b.time+60<=as_of}
        for name,start,end in choices:
            if not start<end<=now: continue
            times=list(range(start,end,60));present=[lookup[t] for t in times if t in lookup]
            missing=len(times)-len(present)
            if not present: continue
            hi=max(b.high for b in present);lo=min(b.low for b in present)
            key=f'{symbol}:WINDOW:{name}:{start}:{end}'
            result[key]=dict(kind='window',symbol=symbol,name=name,start=start,end=end,
              available_at=end,complete=not missing,missing_minutes=missing,
              high=hi,low=lo,high_times=[b.time for b in present if b.high==hi],
              low_times=[b.time for b in present if b.low==lo],
              selection_status='fixed research candidate, not confirmed trader anchor')
    return result


def _window(packet,key):
    e=packet['evidence'].get(key)
    if not e or e['kind']!='window' or e['available_at']>packet['as_of']:
        raise ValueError('Window reference missing or future')
    return e


def measure_smt(packet, selection, direction):
    expected={'reference_es','reference_mnq','current_es','current_mnq','side'}
    if not isinstance(selection,dict) or set(selection)!=expected:
        raise ValueError('SMT selection needs four window IDs and side')
    side=selection['side']
    if side not in ('low','high') or direction not in ('long','short'):
        raise ValueError('SMT direction and side required')
    if (side=='low') != (direction=='long'):
        raise ValueError('Selected SMT side opposes plan direction')
    windows={key:_window(packet,selection[key]) for key in expected-{'side'}}
    for phase in ('reference','current'):
        a,b=windows[phase+'_es'],windows[phase+'_mnq']
        if a['symbol']!='ES' or b['symbol']!='MNQ' or (a['start'],a['end'])!=(b['start'],b['end']):
            raise ValueError('SMT requires matching ES/MNQ intervals')
    ref,cur=windows['reference_es'],windows['current_es']
    if not ref['start']<ref['end']<=cur['start']<cur['end']:
        raise ValueError('SMT windows must be disjoint and ordered')
    if cur['end']!=packet['as_of']//60*60:
        raise ValueError('Current window must extend to latest closed minute; stale SMT rejected')
    result=dict(available_at=cur['end'],selected_at=packet['as_of'],side=side,
                selection=selection,selection_fidelity='unverified',equality_policy='strict sweep; equal touches reported separately')
    if any(not w['complete'] for w in windows.values()):
        return dict(result,status='missing_paired_data',intact=None,extrema={})
    extrema={}
    for symbol,suffix in [('ES','es'),('MNQ','mnq')]:
        r,c=windows['reference_'+suffix],windows['current_'+suffix]
        swept=c[side]<r[side] if side=='low' else c[side]>r[side]
        extrema[symbol]=dict(reference=r[side],current=c[side],swept=swept,equal=c[side]==r[side],
          reference_times=r[side+'_times'],current_times=c[side+'_times'])
    a,b=extrema['ES']['swept'],extrema['MNQ']['swept']
    status='divergence' if a!=b else 'both_swept' if a else 'neither_swept'
    # Whether Tanya treats an equal touch as a sweep is unresolved. Don't turn
    # that unresolved convention into a positive/negative engine fact.
    intact=None if any(x['equal'] for x in extrema.values()) else a!=b
    return dict(result,status=status,intact=intact,extrema=extrema)


def measure_range(packet, selection):
    if not isinstance(selection,dict) or set(selection)!={'window_id','origin_bar_id','side'}:
        raise ValueError('Range selection needs window, origin bar and side')
    w=_window(packet,selection['window_id']);b=packet['evidence'].get(selection['origin_bar_id'])
    side=selection['side']
    if side not in ('low','high') or not b or b['kind']!='bar' or b['symbol']!=w['symbol']:
        raise ValueError('Range origin must be a same-instrument candle')
    if not w['start']<=b['time']<b['available_at']<=w['end']<=packet['as_of']:
        raise ValueError('Origin must be contained within the selected historical range')
    price=b[side];span=w['high']-w['low']
    return dict(available_at=w['end'],selected_at=packet['as_of'],selection=selection,
       complete=w['complete'],range_low=w['low'],range_high=w['high'],origin_price=price,
       normalized_origin=(price-w['low'])/span if w['complete'] and span>0 else None,
       qualifies_as_range_extreme=None,selection_fidelity='unverified',
       reason='Geometry only; no numerical extreme threshold attributed to Tanya')


def level_catalog(markets, as_of):
    """Directly observable opens, separate from inferred support/resistance.

    Weekly open convention is Sunday 18:00 ET. If that minute is missing, the
    level is absent: never substitute the next recorded candle's open.
    """
    local=datetime.fromtimestamp(as_of,ET)
    midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
    sunday=midnight-timedelta(days=(local.weekday()+1)%7)
    week=sunday.replace(hour=18)
    if week>local:week-=timedelta(days=7)
    times={'weekly_open':int(week.timestamp()),'open_0830':int(midnight.replace(hour=8,minute=30).timestamp()),
           'open_0930':int(midnight.replace(hour=9,minute=30).timestamp())}
    out={}
    for symbol,bars in markets.items():
        lookup={b.time:b for b in bars if b.time+60<=as_of}
        for name,t in times.items():
            if t not in lookup:continue
            b=lookup[t]
            out[f'{symbol}:LEVEL:{name}:{t}']=dict(kind='level',symbol=symbol,name=name,price=b.open,
              available_at=t+60,source_bar_start=t,source_field='open',
              convention='Sunday 18:00 ET' if name=='weekly_open' else 'ET clock open',
              source_ohlc=dict(open=b.open,high=b.high,low=b.low,close=b.close))
    return out
