"""Incremental, receipt-aware observations. Geometry conventions are research choices."""
import csv
import io
import math
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .common import digest, read_bytes

NY = ZoneInfo('America/New_York')
SPECS = {'NQ':dict(tick=.25,multiplier=20,tick_value=5),
         'MNQ':dict(tick=.25,multiplier=2,tick_value=.5),
         'ES':dict(tick=.25,multiplier=50,tick_value=12.5)}
TFS = (1,5,15,60,240,1440)

def clock(ts):
    return datetime.fromtimestamp(ts, NY)

def validate_bar(b):
    fields = {'symbol','contract','ts','received_at','open','high','low','close','source'}
    if not isinstance(b,dict) or set(b)!=fields or b['symbol'] not in SPECS:
        raise ValueError('BAR_SCHEMA')
    if not isinstance(b['contract'],str) or not b['contract'] or not isinstance(b['source'],str):
        raise ValueError('CONTRACT_REQUIRED')
    if type(b['ts']) is not int or b['ts']%60 or type(b['received_at']) is not int or b['received_at']<b['ts']+60:
        raise ValueError('BAR_TIME')
    for k in ('open','high','low','close'):
        if type(b[k]) not in (int,float) or not math.isfinite(b[k]) or b[k]<=0 or abs(b[k]*4-round(b[k]*4))>1e-6:
            raise ValueError('BAR_PRICE')
    if not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:
        raise ValueError('BAR_GEOMETRY')
    return b

def bar_id(b):
    return f"{b['symbol']}:{b['contract']}:M1:{b['ts']}"

def load_csv(path, symbol):
    raw = read_bytes(path)
    bars = []
    last = -1
    for r in csv.DictReader(io.StringIO(raw.decode())):
        b=dict(symbol=symbol,contract='archive-id:'+r['instrument_id'],ts=int(r['timestamp']),
               received_at=int(r['timestamp'])+60,source='ARCHIVE_OHLC_ASSUMED_CLOSE_AVAILABILITY',
               **{k:float(r[k]) for k in ('open','high','low','close')})
        validate_bar(b)
        if b['ts']<=last:raise ValueError('CSV_DUPLICATE_OR_UNSORTED')
        last=b['ts'];bars.append(b)
    return bars,dict(path=str(path),sha256=__import__('hashlib').sha256(raw).hexdigest(),rows=len(bars),
        contracts=sorted({b['contract'] for b in bars}),receipts='ASSUMED_BAR_CLOSE_NOT_MEASURED',
        instrument_definitions_verified=False,price_substitution=False)

def bounds(ts, tf):
    if tf<60:return ts//(tf*60)*(tf*60), (ts//(tf*60)+1)*(tf*60)
    local=clock(ts)
    anchor=local.replace(hour=18,minute=0,second=0,microsecond=0)
    if local<anchor:anchor-=timedelta(days=1)
    start=int(anchor.timestamp())
    if tf==1440:
        # Electronic trading day, excluding the 17–18 NY maintenance break.
        return start,int((anchor+timedelta(days=1)).replace(hour=17).timestamp())
    start+=((ts-start)//(tf*60))*(tf*60)
    return start,start+tf*60

def fresh_state(symbol,contract):
    return dict(symbol=symbol,contract=contract,last_ts=None,buckets={},frames={str(tf):[] for tf in TFS},
                levels=[],zones=[],last_events=[],gap_count=0,rth=None,previous_rth=None)

def event(state, kind, frame, refs=(), **data):
    value=dict(kind=kind,symbol=state['symbol'],contract=state['contract'],timeframe=frame['timeframe'],
               ts=frame['ts'],available_at=frame['available_at'],evidence_ids=list(refs),
               status='CONFIRMED_GEOMETRY',**data)
    value['id']=digest(value)[:24]
    return value

def completed_week(frames):
    """Five observed full electronic sessions; holiday-shortened weeks stay unknown."""
    if len(frames)<5:return None
    week=frames[-5:];last=clock(week[-1]['end'])
    if last.weekday()!=4 or (last.hour,last.minute)!=(17,0):return None
    monday=last.date()-timedelta(days=4)
    identity=(week[-1]['symbol'],week[-1]['contract'])
    for i,f in enumerate(week):
        end=clock(f['end']);start=clock(f['ts'])
        if ((f['symbol'],f['contract'])!=identity or f['timeframe']!=1440 or
            end.date()!=monday+timedelta(days=i) or (end.hour,end.minute)!=(17,0) or
            start!=end.replace(hour=18)-timedelta(days=1) or f['available_at']<f['end']):return None
    return dict(start=week[0]['ts'],end=week[-1]['end'],available_at=max(f['available_at'] for f in week),
        high=max(f['high'] for f in week),low=min(f['low'] for f in week),
        evidence_ids=[f['id'] for f in week],scope='FIVE_COMPLETE_18_TO_17_NY_SESSIONS',
        holiday_calendar_verified=False)

def week_context(state,as_of):
    local=clock(as_of);friday=local.date()-timedelta(days=(local.weekday()-4)%7)
    if local.weekday()==4 and (local.hour,local.minute)<(17,0):friday-=timedelta(days=7)
    week=state.get('previous_week')
    if not week or week['available_at']>as_of:
        return dict(status='UNAVAILABLE_INCOMPLETE_OR_NO_HISTORY',expected_week_end=friday.isoformat())
    return dict(week,status='LATEST_COMPLETE_WEEK' if clock(week['end']).date()==friday else 'LAST_AVAILABLE_WEEK_NOT_PREVIOUS',expected_week_end=friday.isoformat())

def advance(state, b):
    """Mutates a transaction-local state; returns immutable new frames/events."""
    validate_bar(b)
    if (b['symbol'],b['contract'])!=(state['symbol'],state['contract']):raise ValueError('CROSS_CONTRACT_STATE')
    if state['last_ts'] is not None and b['ts']<=state['last_ts']:raise ValueError('OUT_OF_ORDER')
    events=[];frames=[]
    base=dict(id=bar_id(b),symbol=b['symbol'],contract=b['contract'],timeframe=1,ts=b['ts'],end=b['ts']+60,
              available_at=b['received_at'],**{k:b[k] for k in ('open','high','low','close')})
    if state['last_ts'] is not None and b['ts']!=state['last_ts']+60:
        events.append(event(state,'DATA_GAP',base,[base['id']],after=state['last_ts']+60,before=b['ts'],
                            interpretation='May include scheduled closure; no missing candles interpolated.'))
        state['gap_count']+=1
        state['zones']=[]
    state['last_ts']=b['ts']
    local=clock(b['ts']);minute=local.hour*60+local.minute;day=local.date().isoformat()
    if state.get('rth') and state['rth']['day']!=day:
        old=state['rth'];state['previous_rth']=old if old['complete'] and old['count']==390 else None
        state['rth']=None
    if 570<=minute<960:
        r=state.get('rth')
        if r is None:
            r=dict(day=day,start=int(local.replace(hour=9,minute=30,second=0).timestamp()),count=0,complete=minute==570,
                high=b['high'],low=b['low'],high_id=base['id'],low_id=base['id'],last=b['ts']-60)
        if b['ts']!=r['last']+60:r['complete']=False
        if b['high']>r['high']:r.update(high=b['high'],high_id=base['id'])
        if b['low']<r['low']:r.update(low=b['low'],low_id=base['id'])
        r.update(count=r['count']+1,last=b['ts'],end=b['ts']+60,available_at=b['received_at']);state['rth']=r
        if minute==959 and r['count']==390 and r['complete']:
            for side in ('HIGH','LOW'):
                field=side.lower()
                e=event(state,'PRIOR_RTH_LEVEL',dict(base,timeframe=390),[r[field+'_id'],base['id']],side=side,price=r[field],scope='COMPLETE_0930_1600_NY')
                events.append(e);state['levels'].append(dict(e,consumed=False))
    for tf in TFS:
        start,end=bounds(b['ts'],tf)
        if not start<=b['ts']<end:continue
        key=str(tf);bucket=state['buckets'].get(key)
        if not bucket or bucket['ts']!=start:
            bucket=dict(ts=start,end=end,count=0,expected=(end-start)//60,next=start,
                        open=b['open'],high=b['high'],low=b['low'],close=b['close'],complete=True,available_at=b['received_at'])
            state['buckets'][key]=bucket
        if b['ts']!=bucket['next']:bucket['complete']=False
        bucket.update(next=b['ts']+60,count=bucket['count']+1,high=max(bucket['high'],b['high']),
                      low=min(bucket['low'],b['low']),close=b['close'],available_at=max(bucket['available_at'],b['received_at']))
        if b['ts']+60!=end or not bucket['complete'] or bucket['count']!=bucket['expected']:continue
        f=dict(id=f"{b['symbol']}:{b['contract']}:M{tf}:{start}",symbol=b['symbol'],contract=b['contract'],timeframe=tf,
               ts=start,end=end,available_at=bucket['available_at'],**{k:bucket[k] for k in ('open','high','low','close')})
        series=state['frames'][key]
        prior=series[-1] if series else None
        contiguous=prior and prior['end']==f['ts']
        # Only levels confirmed before this bar opened can be crossed by it.
        for level in state['levels']:
            if (level['timeframe']!=tf and not (tf==1 and level['timeframe']>1)) or level['consumed'] or level['available_at']>f['ts']:continue
            side=level['side'];p=level['price']
            crossed=f['high']>p if side=='HIGH' else f['low']<p
            if crossed:
                reclaimed=f['close']<p if side=='HIGH' else f['close']>p
                e=event(state,'SWEEP' if reclaimed else 'STRUCTURE_BREAK',f,[level['id'],f['id']],
                    side=side,direction=('SHORT' if side=='HIGH' else 'LONG') if reclaimed else ('LONG' if side=='HIGH' else 'SHORT'),
                    price=p,extreme=f['high'] if side=='HIGH' else f['low'],level_kind=level['kind'],level_timeframe=level['timeframe'],
                    liquidity_status='STOP_LOCATION_HYPOTHESIS_FROM_PRICE_NOT_ORDER_BOOK')
                events.append(e);level['consumed']=True;level['consumed_at']=f['available_at']
        for zone in state['zones']:
            if zone['timeframe']!=tf or zone['inverted'] or zone['available_at']>f['ts']:continue
            if (zone['direction']=='LONG' and f['close']<zone['lower']) or (zone['direction']=='SHORT' and f['close']>zone['upper']):
                zone['inverted']=True;zone['inverted_at']=f['available_at']
                events.append(event(state,'IFVG',f,[zone['id'],f['id']],lower=zone['lower'],upper=zone['upper'],
                                    direction='SHORT' if zone['direction']=='LONG' else 'LONG'))
        if contiguous and len(series)>=2 and series[-2]['end']==prior['ts']:
            a=series[-2];c=f
            direction='LONG' if c['low']>a['high'] else 'SHORT' if c['high']<a['low'] else None
            if direction:
                z=event(state,'FVG',f,[a['id'],prior['id'],c['id']],direction=direction,
                    lower=a['high'] if direction=='LONG' else c['high'],upper=c['low'] if direction=='LONG' else a['low'])
                state['zones'].append(dict(z,inverted=False));events.append(z)
            for side, field, comp in [('HIGH','high',lambda x,y:x>y),('LOW','low',lambda x,y:x<y)]:
                if comp(prior[field],a[field]) and comp(prior[field],c[field]):
                    pivot=event(state,'PIVOT',f,[a['id'],prior['id'],c['id']],side=side,price=prior[field],pivot_ts=prior['ts'])
                    old=next((l for l in reversed(state['levels']) if l['timeframe']==tf and l['side']==side and l['kind']=='PIVOT'),None)
                    if old and abs(old['price']-pivot['price'])<=.25:
                        events.append(event(state,'EQUAL_EXTREMES',f,[old['id'],pivot['id']],side=side,price=pivot['price'],
                                            tolerance_ticks=1,liquidity_status='HYPOTHESIS_NOT_MEASURED_STOPS'))
                    state['levels'].append(dict(pivot,consumed=False));events.append(pivot)
        if contiguous and len(series)>=20 and all(y['end']==z['ts'] for y,z in zip(series[-20:],series[-19:])):
            span=f['high']-f['low'];median=statistics.median(x['high']-x['low'] for x in series[-20:])
            if span>=median*1.5 and span>0 and abs(f['close']-f['open'])>=span*.6:
                events.append(event(state,'DISPLACEMENT',f,[f['id']]+[x['id'] for x in series[-20:]],
                    direction='LONG' if f['close']>f['open'] else 'SHORT',range=span,prior_median=median,
                    definition='Research: range >=1.5x prior20 median and body>=60%; not a universal ICT threshold.'))
        if tf==1440:
            for side,field in [('HIGH','high'),('LOW','low')]:
                e=event(state,'PRIOR_SESSION_LEVEL',f,[f['id']],side=side,price=f[field],scope='COMPLETE_18_TO_17_NY_SESSION')
                state['levels'].append(dict(e,consumed=False));events.append(e)
            week=completed_week(series+[f])
            if week:
                state['previous_week']=week
                for side in ('HIGH','LOW'):
                    e=event(state,'PRIOR_WEEK_LEVEL',dict(f,timeframe=10080,available_at=week['available_at']),
                        week['evidence_ids'],side=side,price=week[side.lower()],scope=week['scope'],
                        week_start=week['start'],week_end=week['end'],holiday_calendar_verified=False)
                    state['levels'].append(dict(e,consumed=False));events.append(e)
        series.append(f);state['frames'][key]=series[-80:];frames.append(f)
    # Keep bounded active market memory, while every original event remains in SQLite.
    # Per-timeframe retention prevents abundant M1 pivots from evicting daily levels.
    state['levels']=[l for tf in (*TFS,390,10080) for l in [x for x in state['levels'] if x['timeframe']==tf][-24:]]
    state['zones']=[z for tf in TFS for z in [x for x in state['zones'] if x['timeframe']==tf][-20:]]
    state['last_events']=(state['last_events']+events)[-100:]
    return frames,events

def context(states, as_of):
    result={}
    for symbol,s in states.items():
        last=s['frames']['1'][-1] if s['frames']['1'] else None
        ranges={}
        for tf in TFS:
            bars=s['frames'][str(tf)]
            if bars:
                # Explicit fixed trailing window, never an asserted discretionary swing range.
                window=bars[-20:];lo=min(x['low'] for x in window);hi=max(x['high'] for x in window)
                ranges[str(tf)]=dict(low=lo,high=hi,equilibrium=(hi+lo)/2,start=window[0]['ts'],end=window[-1]['end'],
                    bars=len(window),complete_bars=True,continuous=all(a['end']==b['ts'] for a,b in zip(window,window[1:])),
                    price_location='PREMIUM' if last['close']>(hi+lo)/2 else 'DISCOUNT',
                    selection='TRAILING_20_COMPLETED_BARS_RESEARCH_WINDOW',evidence_ids=[b['id'] for b in window])
        result[symbol]=dict(contract=s['contract'],latest=last,ranges=ranges,
            active_levels=[l for tf in (*TFS,390,10080) for l in [x for x in s['levels'] if not x['consumed'] and x['timeframe']==tf][-6:]],
            active_zones=[z for tf in TFS for z in [x for x in s['zones'] if not x['inverted'] and x['timeframe']==tf][-4:]],
            rth=s.get('rth'),previous_rth=s.get('previous_rth'),previous_week=week_context(s,as_of),
            stale=last is None or as_of-last['end']>120,
            gaps=s['gap_count'],provisional={tf:dict(v,used_for_signal=False) for tf,v in s['buckets'].items() if v['end']>as_of},
            unavailable=['verified OB/breaker selection','measured resting stops','full dated exchange holiday calendar','economic news'])
    return result

def intermarket(states,as_of):
    """Fixed synchronized RTH-window comparison; never inferred from unequal windows."""
    if not {'NQ','ES'}<=states.keys():return dict(status='MISSING_PAIRED_MARKETS',events=[])
    windows=[states[s].get(k) for k in ('rth','previous_rth') for s in ('NQ','ES')]
    if any(not w or not w['complete'] for w in windows):return dict(status='INCOMPLETE_WINDOWS',events=[])
    nq,es,pn,pe=windows
    if (nq['start'],nq['end'])!=(es['start'],es['end']) or (pn['start'],pn['end'])!=(pe['start'],pe['end']) or not 0<=as_of-nq['end']<=120 or pn['end']>nq['start']:
        return dict(status='UNSYNCHRONIZED_WINDOWS',events=[])
    sides={}
    for side in ('high','low'):
        a=nq[side]>pn[side] if side=='high' else nq[side]<pn[side]
        b=es[side]>pe[side] if side=='high' else es[side]<pe[side]
        sides[side]=dict(nq_crossed=a,es_crossed=b,divergence=a!=b,
            equal_touch=nq[side]==pn[side] or es[side]==pe[side],
            evidence_ids=[w[side+'_id'] for w in windows])
    return dict(status='SYNCHRONIZED_FIXED_WINDOW_GEOMETRY',current_start=nq['start'],current_end=nq['end'],
        reference_start=pn['start'],reference_end=pn['end'],sides=sides,
        interpretation='Possible SMT evidence for these explicit windows, not a verified discretionary anchor selection or trade trigger.')
