"""Automatic, causal LONG research policy. Numerical choices are hypotheses,
not verified Tanja rules. Only closed ES/NQ/MNQ bars; no model or broker calls.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo
import math
from entry_rules import compile_entry, fingerprint

NY=ZoneInfo('America/New_York')
VERSION='tanja-auto-long-research-v1'

@dataclass(frozen=True)
class Policy:
    # Engineering proxies, explicitly versioned rather than attributed to Tanja.
    body_fraction: float=.60
    close_location: float=.75
    expansion_ratio: float=1.0
    strength_lookback: int=5
    extreme_fraction: float=.35
    sweep_age_minutes: int=30
    gap_age_minutes: int=30
    stop_buffer_ticks: int=1
    context_ttl_seconds: int=60
    max_delay_seconds: int=15
    timeframe_priority: tuple=(5,3,2,1)
    projection_ratio: float=1.0


def valid_price(x):
    return type(x) in (int,float) and math.isfinite(x) and x>0 and x*4==round(x*4)


def normalized(markets,cutoff,now):
    result={}
    for symbol in ('ES','NQ','MNQ'):
        out=[];seen=set()
        for raw in markets.get(symbol,[]):
            t=raw.get('time')
            if type(t) is not int or t<0 or t%60:raise ValueError('Invalid M1 timestamp')
            # Later bars are never usable in this frozen decision, even if supplied.
            if t+60>cutoff:continue
            received=raw.get('received_at',t+60)
            if type(received) not in (int,float) or not math.isfinite(received) or received<t+60:
                raise ValueError('Invalid receipt time')
            if received>now:continue
            if t in seen:raise ValueError('Duplicate bar timestamp')
            seen.add(t)
            if any(not valid_price(raw.get(k)) for k in ('open','high','low','close')):raise ValueError('Invalid OHLC price')
            if not raw['low']<=min(raw['open'],raw['close'])<=max(raw['open'],raw['close'])<=raw['high']:raise ValueError('Impossible OHLC')
            out.append(dict(time=t,available_at=t+60,**{k:raw[k] for k in ('open','high','low','close')}))
        result[symbol]=sorted(out,key=lambda b:b['time'])
    return result


def complete(bars,start,end):
    rows=[b for b in bars if start<=b['time']<end]
    return rows if [b['time'] for b in rows]==list(range(start,end,60)) else []


def aggregate(bars,tf):
    # ET midnight anchored intraday buckets. Reject DST/maintenance gaps rather
    # than stretching/interpolating a candle. This anchor is an explicit policy.
    groups={}
    for b in bars:
        dt=datetime.fromtimestamp(b['time'],NY)
        origin=int(dt.replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        start=origin+((b['time']-origin)//(tf*60))*(tf*60)
        groups.setdefault(start,[]).append(b)
    out=[]
    for start,g in sorted(groups.items()):
        if [b['time'] for b in g]!=list(range(start,start+tf*60,60)):continue
        out.append(dict(time=start,available_at=start+tf*60,open=g[0]['open'],high=max(x['high'] for x in g),low=min(x['low'] for x in g),close=g[-1]['close']))
    return out


def pivots(bars,side):
    # Strict one-left/one-right convention; confirmation is after the right close.
    out=[]
    for a,b,c in zip(bars,bars[1:],bars[2:]):
        if b['time']-a['time']!=c['time']-b['time']:continue
        if b[side]>max(a[side],c[side]) if side=='high' else b[side]<min(a[side],c[side]):
            out.append(dict(time=b['time'],available_at=c['available_at'],price=b[side]))
    return out


def zones(bars,tf,side):
    out=[]
    for i in range(2,len(bars)):
        a,b,c=bars[i-2:i+1]
        if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60:continue
        formed=c['low']>a['high'] if side=='bull' else c['high']<a['low']
        if not formed:continue
        lo,hi=(a['high'],c['low']) if side=='bull' else (c['high'],a['low'])
        out.append(dict(time=c['time'],available_at=c['available_at'],lower=lo,upper=hi,timeframe=tf,side=side))
    return out


def _policy_check(p):
    if not 0<p.body_fraction<=1 or not 0<p.close_location<=1 or not 0<=p.extreme_fraction<=1:raise ValueError('Invalid normalized policy')
    if not math.isfinite(p.expansion_ratio) or p.expansion_ratio<=0:raise ValueError('Invalid expansion policy')
    for k in ('strength_lookback','sweep_age_minutes','gap_age_minutes','context_ttl_seconds','max_delay_seconds'):
        if type(getattr(p,k)) is not int or getattr(p,k)<=0:raise ValueError('Invalid policy '+k)
    if type(p.stop_buffer_ticks) is not int or p.stop_buffer_ticks<0:raise ValueError('Invalid stop buffer')
    if not p.timeframe_priority or len(set(p.timeframe_priority))!=len(p.timeframe_priority) or any(type(t) is not int or t not in (1,2,3,5) for t in p.timeframe_priority):raise ValueError('Invalid timeframe policy')
    if p.projection_ratio not in (.5,1):raise ValueError('Unsupported projection policy')


def select(markets,*,cutoff,now,risk_budget_usd,max_contracts,policy=None):
    """No annotated setup, strength, stop or target required. Account risk is
    explicit caller configuration, not inferred from stale balance screenshots.
    Returns automatic research decisions and compiler output, never orders.
    """
    p=policy or Policy();_policy_check(p)
    if type(cutoff) is not int or cutoff%60 or type(now) is not int or now<cutoff:raise ValueError('Invalid decision clock')
    if type(risk_budget_usd) not in (int,float) or not math.isfinite(risk_budget_usd) or risk_budget_usd<=0 or type(max_contracts) is not int or max_contracts<1:raise ValueError('Explicit paper risk configuration required')
    data=normalized(markets,cutoff,now)
    result=dict(version=VERSION,policy=asdict(p),mode='AUTOMATIC_RESEARCH_ONLY',orders_enabled=False,
                semantic_fidelity_verified=False,automatic_selection=True,state='ABSTAIN',reasons=[],candidates=[],selected=None,
                input_hash=fingerprint(data),cutoff=cutoff)
    if now-cutoff>p.max_delay_seconds:result['reasons']=['STALE_DECISION'];return result
    day=datetime.fromtimestamp(cutoff,NY).replace(hour=0,minute=0,second=0,microsecond=0)
    start=int(day.replace(hour=9,minute=30).timestamp());end=int(day.replace(hour=11).timestamp())
    if day.weekday()>4 or not start<cutoff<=end:result['reasons']=['OUTSIDE_RESEARCH_SESSION'];return result
    if any(not v or v[-1]['available_at']!=cutoff for v in data.values()):result['reasons']=['MISSING_SYNCHRONIZED_ES_NQ_MNQ'];return result
    current={s:complete(v,start,cutoff) for s,v in data.items()}
    if any(not v for v in current.values()):result['reasons']=['INCOMPLETE_RTH_HISTORY'];return result
    overnight_start=int(day.timestamp())
    windows=[('overnight',overnight_start,start,2)]
    # Previous RTH is available only with all 390 minutes. Weekends skipped;
    # unknown holidays yield no level, never a fabricated previous-day extreme.
    prev=day-timedelta(days=1)
    while prev.weekday()>4:prev-=timedelta(days=1)
    windows.append(('previous_rth',int(prev.replace(hour=9,minute=30).timestamp()),int(prev.replace(hour=16).timestamp()),3))
    refs={}
    for name,a,b,rank in windows:
        rows={s:complete(v,a,b) for s,v in data.items()}
        if all(rows.values()):refs[name]=dict(rows=rows,start=a,end=b,rank=rank)
    nq=data['NQ'];mnq=data['MNQ'];es=data['ES'];nq_now=nq[-1];entry=mnq[-1]['close']
    ev={}
    def add(key,item):ev[key]=item;return key
    def level(key,symbol,price,at,**extra):return add(key,dict(kind='level',symbol=symbol,price=price,available_at=at,**extra))
    series={}
    for s,rows in data.items():
        for tf in (1,2,3,5,60,240):
            bs=rows if tf==1 else aggregate(rows,tf)
            series[s,tf]=bs
            for b in bs:add(f'{s}:M{tf}:{b["time"]}',dict(kind='bar',symbol=s,timeframe=tf,**b))
    # Liquidity reclaim POIs, detected before candidate selection.
    pois=[]
    for name,ref in refs.items():
        low=min(b['low'] for b in ref['rows']['NQ'])
        swept=[b for b in current['NQ'] if b['low']<low]
        if not swept or nq_now['close']<=low:continue
        extreme=min(swept,key=lambda b:(b['low'],-b['time']))
        if cutoff-extreme['available_at']>60*p.sweep_age_minutes:continue
        # Same matched reference/current intervals for ES/NQ SMT.
        es_low=min(b['low'] for b in ref['rows']['ES']);es_cur=min(b['low'] for b in current['ES'])
        smt=es_cur>es_low # equality is deliberately not positive evidence
        refid=level(f'NQ:REF:{name}:low','NQ',low,ref['end'])
        pois.append(dict(kind='liquidity_reclaim',rank=ref['rank'],name=name,ref=refid,extreme=extreme,requires_smt=False,smt=smt,available_at=extreme['available_at']))
    # Bullish HTF gap recipe requires a recent return plus paired divergence.
    for tf in (240,60):
        for z in zones(series['NQ',tf],tf,'bull'):
            if z['available_at']>start:continue
            later=[b for b in nq if z['available_at']<=b['time']]
            if any(b['close']<z['lower'] for b in later):continue
            touches=[b for b in current['NQ'] if b['low']<=z['upper'] and b['high']>=z['lower']]
            if not touches:continue
            reaction=min(touches,key=lambda b:(b['low'],-b['time']))
            if cutoff-reaction['available_at']>60*p.sweep_age_minutes or nq_now['close']<=z['upper']:continue
            matching=[poi for poi in pois if poi['smt']]
            if not matching:continue
            refid=add(f'NQ:HTF:{tf}:{z["time"]}',dict(kind='HTF_FVG',symbol='NQ',**z))
            pois.append(dict(kind='htf_gap_smt',rank=5 if tf==240 else 4,name=f'M{tf}_gap',ref=refid,extreme=reaction,requires_smt=True,smt=True,available_at=reaction['available_at']))
    if not pois:result['reasons']=['NO_SUPPORTED_LIQUIDITY_RECLAIM_OR_HTF_SMT_RECIPE'];return result
    # Targets are selected on NQ context, priced independently on matching MNQ
    # intervals, not by applying a futures-series price offset.
    targets=[]
    for name,ref in refs.items():
        hi=max(b['high'] for b in ref['rows']['NQ'])
        if max(b['high'] for b in current['NQ'])>=hi:continue
        price=max(b['high'] for b in ref['rows']['MNQ'])
        rid=level(f'MNQ:REF:{name}:high','MNQ',price,ref['end'])
        if price>entry:targets.append(dict(price=price,id=rid,kind='level',reason=f'Untouched NQ {name} high; own MNQ window high'))
    for pivot in pivots(nq,'high'):
        after=[b for b in nq if b['time']>=pivot['available_at']]
        if not after or any(b['high']>=pivot['price'] for b in after):continue
        own=next((b for b in mnq if b['time']==pivot['time']),None)
        if own and own['high']>entry:
            rid=level(f'MNQ:SWING_TARGET:{pivot["time"]}','MNQ',own['high'],pivot['available_at'])
            if not any(b['high']>=own['high'] for b in mnq if b['time']>=pivot['available_at']):
                targets.append(dict(price=own['high'],id=rid,kind='level',reason='Untouched confirmed NQ swing; matched MNQ bar high'))
    for tf in (60,240):
        nz=zones(series['NQ',tf],tf,'bear');mz=zones(series['MNQ',tf],tf,'bear')
        for z in nz:
            own=next((q for q in mz if q['available_at']==z['available_at']),None)
            if not own or own['lower']<=entry:continue
            if any(b['high']>=z['lower'] for b in nq if b['time']>=z['available_at']):continue
            if any(b['high']>=own['lower'] for b in mnq if b['time']>=own['available_at']):continue
            rid=level(f'MNQ:OPPOSING:{tf}:{z["time"]}','MNQ',own['lower'],own['available_at'])
            targets.append(dict(price=own['lower'],id=rid,kind='level',reason='Nearest still-untouched opposing HTF gap boundary'))
    for tf in p.timeframe_priority:
        bars=series['NQ',tf]
        if len(bars)<max(p.strength_lookback+1,4) or bars[-1]['available_at']!=cutoff:continue
        trigger=bars[-1];prior=bars[-p.strength_lookback-1:-1]
        if [b['time'] for b in prior+[trigger]]!=list(range(prior[0]['time'],cutoff,tf*60)):continue
        span=trigger['high']-trigger['low'];baseline=median(b['high']-b['low'] for b in prior)
        strength=dict(body_fraction=(trigger['close']-trigger['open'])/span if span else 0,
                      close_location=(trigger['close']-trigger['low'])/span if span else 0,
                      range_expansion=span/baseline if baseline else 0)
        strong=(strength['body_fraction']>=p.body_fraction and strength['close_location']>=p.close_location and baseline>0 and strength['range_expansion']>=p.expansion_ratio)
        for i in range(2,len(bars)-1):
            a,b,c=bars[i-2:i+1]
            if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60 or c['high']>=a['low']:continue
            if cutoff-c['available_at']>p.gap_age_minutes*60:continue
            path=bars[i+1:]
            if [q['time'] for q in path]!=list(range(c['available_at'],cutoff,tf*60)):continue
            if trigger['close']<=a['low'] or any(q['close']>a['low'] for q in path[:-1]):continue
            for poi in pois:
                event=poi['extreme']
                # The gap must form during the same excursion (not an old gap
                # unrelated to today's sweep). Trigger cannot precede the POI.
                if not a['time']<=event['time']<=trigger['time']:continue
                recent=complete(nq,max(start,trigger['time']-30*60),trigger['time'])
                if not recent:continue
                rlo=min(x['low'] for x in recent);rhi=max(x['high'] for x in recent)
                origin=(c['low']-rlo)/(rhi-rlo) if rhi>rlo else 1
                faults=[]
                if not strong:faults.append('WEAK_CONFIRMATION_PROXY')
                if origin>p.extreme_fraction:faults.append('INVERSION_NOT_NEAR_RANGE_LOW')
                swings=[x for x in pivots(bars,'high') if x['available_at']<=trigger['time'] and event['time']-30*60<=x['time']<=event['time']]
                swing=max(swings,key=lambda x:x['time']) if swings else None
                if not swing or trigger['close']<=swing['price']:faults.append('SELECTED_HIGH_NOT_BROKEN_ON_CLOSE')
                # Initial stop is below the entire paired MNQ reaction window,
                # not a post-entry trail or a copied NQ low.
                reaction=complete(mnq,event['time'],cutoff)
                anchor=min(reaction,key=lambda x:(x['low'],x['time']))
                stop=anchor['low']-.25*p.stop_buffer_ticks
                target=min(targets,key=lambda x:(x['price'],x['id'])) if targets else None
                if not target and swing:
                    own=next((x for x in mnq if x['time']==swing['time']),None)
                    # Use the complete matching TF candle for a TF swing.
                    own=next((x for x in series['MNQ',tf] if x['time']==swing['time']),None)
                    extreme=min(reaction,key=lambda x:(x['low'],x['time']))
                    if own and own['available_at']<=extreme['available_at']:
                        value=own['high']+p.projection_ratio*(own['high']-extreme['low'])
                        if valid_price(value) and value>entry:
                            rid=level(f'MNQ:PROJECTION:{tf}:{own["time"]}:{extreme["time"]}','MNQ',value,cutoff)
                            target=dict(price=value,id=rid,kind='level',reason='False-move projection fallback; fixed research ratio',projection=dict(ratio=p.projection_ratio,origin=own['high'],extreme=extreme['low']))
                if not target:faults.append('NO_UNTOUCHED_TARGET_OR_VALID_PROJECTION')
                if not stop<entry:faults.append('INVALID_STRUCTURAL_STOP')
                qty=min(max_contracts,int(risk_budget_usd/((entry-stop)*2))) if stop<entry else 0
                if qty<1:faults.append('RISK_BUDGET_TOO_SMALL')
                audit=dict(timeframe=tf,poi=poi['name'],recipe=poi['kind'],poi_rank=poi['rank'],poi_reference=poi['ref'],
                           strength=strength,range_origin=origin,smt=poi['smt'],stop_anchor_time=anchor['time'],stop=stop,
                           target=target,quantity=qty,reasons=faults,trigger_time=trigger['time'],gap_time=c['time'])
                result['candidates'].append(audit)
                if faults:continue
                swingid=add(f'NQ:AUTO_SWING:{tf}:{swing["time"]}',dict(kind='pivot',symbol='NQ',side='high',**swing))
                selection=dict(direction='long',execution_symbol='MNQ',analysis_symbol='NQ',selected_at=now,timeframe_minutes=tf,
                    gap_bar_ids=[f'NQ:M{tf}:{x["time"]}' for x in (a,b,c)],trigger_bar_id=f'NQ:M{tf}:{trigger["time"]}',
                    requires_smt=poi['requires_smt'],requires_swing_break=True,requires_followthrough=False,swing_id=swingid,swing_break_mode='close',
                    entry_mode='immediate_after_confirmation',entry_mode_reason=poi['kind']+' plus closed strong-inversion proxy and high break',
                    initial_stop=dict(mode='selected_structural_anchor',anchors=[dict(evidence_id=f'MNQ:M1:{anchor["time"]}',field='low')],buffer_ticks=p.stop_buffer_ticks,reason='Lowest MNQ low during selected reaction; causal structural-invalidation proxy'),
                    target=dict(kind='level',anchor=dict(evidence_id=target['id'],field='price'),reason=target['reason']),
                    entry_reference=dict(evidence_id=f'MNQ:M1:{mnq[-1]["time"]}',field='close'),initial_quantity=qty,risk_budget_usd=risk_budget_usd,context_valid_until=cutoff+p.context_ttl_seconds)
                audit['_selection']=selection
    ready=[x for x in result['candidates'] if not x['reasons']]
    if not ready:result['reasons']=['NO_COMPLETE_AUTOMATIC_CANDIDATE'];return result
    # HTF POI > completed-session low > overnight low; then chosen TF priority;
    # then most recent gap. Stable, inspectable ranking; no profit optimization.
    ready.sort(key=lambda x:(-x['poi_rank'],p.timeframe_priority.index(x['timeframe']),-x['gap_time'],x['poi']))
    chosen=ready[0];selection=chosen['_selection']
    packet=dict(input_mode='market_only',labels_included=False,clock_id=day.strftime('%Y-%m-%d')+':unix_utc',as_of=cutoff,evidence=ev)
    packet['packet_id']=fingerprint(packet)
    selection.update(id=f'{VERSION}:{cutoff}:{chosen["timeframe"]}:{fingerprint(asdict(p))[:12]}',packet_id=packet['packet_id'],clock_id=packet['clock_id'])
    observations=[]
    for f in ('bullish_thesis_supported','poi_reached','sellside_event_observed','nasdaq_bullish_confirmation','target_identified','selected_smt_present'):
        observations.append(dict(field=f,value=chosen['smt'] if f=='selected_smt_present' else True,available_at=cutoff,clock_id=packet['clock_id'],packet_id=packet['packet_id'],source=VERSION+' computed proxy',evidence_ids=[chosen['poi_reference'],selection['trigger_bar_id']]))
    compiled=compile_entry(packet,selection,observations,now=now)
    for item in result['candidates']:item.pop('_selection',None)
    result.update(state='PLAN_READY' if compiled['plan'] else 'COMPILER_BLOCKED',selected=dict(audit=chosen,selection=selection,observations=observations),compiled=compiled,packet=packet)
    return result
