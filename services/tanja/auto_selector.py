"""Automatic, causal directional research policy. Numerical choices are hypotheses,
not verified Tanja rules. Only closed ES/NQ/MNQ bars; no model or broker calls.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo
import math
from entry_rules import compile_entry, fingerprint

NY=ZoneInfo('America/New_York')
VERSION='tanja-auto-directional-research-v2'

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
        if b['time']!=a['available_at'] or c['time']!=b['available_at']:continue
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


def _select_direction(markets,*,cutoff,now,risk_budget_usd,max_contracts,policy=None,direction="long"):
    """No annotated setup, strength, stop or target required. Account risk is
    explicit caller configuration, not inferred from stale balance screenshots.
    Returns automatic research decisions and compiler output, never orders.
    """
    short=direction=='short';sign=-1 if short else 1
    adverse='high' if short else 'low';favorable='low' if short else 'high'
    gap_side='bear' if short else 'bull'
    p=policy or Policy();_policy_check(p)
    if type(cutoff) is not int or cutoff%60 or type(now) is not int or now<cutoff:raise ValueError('Invalid decision clock')
    if type(risk_budget_usd) not in (int,float) or not math.isfinite(risk_budget_usd) or risk_budget_usd<=0 or type(max_contracts) is not int or max_contracts<1:raise ValueError('Explicit paper risk configuration required')
    data=normalized(markets,cutoff,now)
    result=dict(version=VERSION,policy=asdict(p),mode='AUTOMATIC_RESEARCH_ONLY',orders_enabled=False,
                semantic_fidelity_verified=False,automatic_selection=True,state='ABSTAIN',reasons=[],candidates=[],selected=None,
                input_hash=fingerprint(data),cutoff=cutoff,direction=direction)
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
        low=min((b[adverse] for b in ref['rows']['NQ']),key=lambda v:sign*v)
        swept=[b for b in current['NQ'] if sign*(b[adverse]-low)<0]
        if not swept or sign*(nq_now['close']-low)<=0:continue
        extreme=min(swept,key=lambda b:(sign*b[adverse],-b['time']))
        if cutoff-extreme['available_at']>60*p.sweep_age_minutes:continue
        # Same matched reference/current intervals for ES/NQ SMT.
        es_low=min((b[adverse] for b in ref['rows']['ES']),key=lambda v:sign*v);es_cur=min((b[adverse] for b in current['ES']),key=lambda v:sign*v)
        smt=sign*(es_cur-es_low)>0 # equality is deliberately not positive evidence
        refid=level(f'NQ:REF:{name}:{adverse}','NQ',low,ref['end'])
        pois.append(dict(kind='liquidity_reclaim',rank=ref['rank'],name=name,ref=refid,extreme=extreme,requires_smt=False,smt=smt,available_at=extreme['available_at']))
    # Directional HTF gap recipe requires a recent return plus paired divergence.
    for tf in (240,60):
        for z in zones(series['NQ',tf],tf,gap_side):
            if z['available_at']>start:continue
            later=[b for b in nq if z['available_at']<=b['time']]
            if any(sign*(b['close']-z['upper' if short else 'lower'])<0 for b in later):continue
            touches=[b for b in current['NQ'] if b['low']<=z['upper'] and b['high']>=z['lower']]
            if not touches:continue
            reaction=min(touches,key=lambda b:(sign*b[adverse],-b['time']))
            if cutoff-reaction['available_at']>60*p.sweep_age_minutes or sign*(nq_now['close']-z['lower' if short else 'upper'])<=0:continue
            matching=[poi for poi in pois if poi['smt']]
            if not matching:continue
            refid=add(f'NQ:HTF:{tf}:{z["time"]}',dict(kind='HTF_FVG',symbol='NQ',**z))
            pois.append(dict(kind='htf_gap_smt',rank=5 if tf==240 else 4,name=f'M{tf}_gap',ref=refid,extreme=reaction,requires_smt=True,smt=True,available_at=reaction['available_at']))
    if not pois:result['reasons']=['NO_SUPPORTED_LIQUIDITY_RECLAIM_OR_HTF_SMT_RECIPE'];return result
    # Targets are selected on NQ context, priced independently on matching MNQ
    # intervals, not by applying a futures-series price offset.
    targets=[]
    for name,ref in refs.items():
        hi=max((b[favorable] for b in ref['rows']['NQ']),key=lambda v:sign*v)
        if max(sign*b[favorable] for b in current['NQ'])>=sign*hi:continue
        price=max((b[favorable] for b in ref['rows']['MNQ']),key=lambda v:sign*v)
        rid=level(f'MNQ:REF:{name}:{favorable}','MNQ',price,ref['end'])
        if sign*(price-entry)>0:targets.append(dict(price=price,id=rid,kind='level',reason=f'Untouched NQ {name} {favorable}; own MNQ window {favorable}'))
    for pivot in pivots(nq,favorable):
        after=[b for b in nq if b['time']>=pivot['available_at']]
        if not after or any(sign*(b[favorable]-pivot['price'])>=0 for b in after):continue
        own=next((b for b in mnq if b['time']==pivot['time']),None)
        if own and sign*(own[favorable]-entry)>0:
            rid=level(f'MNQ:SWING_TARGET:{pivot["time"]}','MNQ',own[favorable],pivot['available_at'])
            if not any(sign*(b[favorable]-own[favorable])>=0 for b in mnq if b['time']>=pivot['available_at']):
                targets.append(dict(price=own[favorable],id=rid,kind='level',reason=f'Untouched confirmed NQ swing; matched MNQ bar {favorable}'))
    for tf in (60,240):
        nz=zones(series['NQ',tf],tf,'bull' if short else 'bear');mz=zones(series['MNQ',tf],tf,'bull' if short else 'bear')
        boundary='upper' if short else 'lower'
        for z in nz:
            own=next((q for q in mz if q['available_at']==z['available_at']),None)
            if not own or sign*(own[boundary]-entry)<=0:continue
            if any(sign*(b[favorable]-z[boundary])>=0 for b in nq if b['time']>=z['available_at']):continue
            if any(sign*(b[favorable]-own[boundary])>=0 for b in mnq if b['time']>=own['available_at']):continue
            rid=level(f'MNQ:OPPOSING:{tf}:{z["time"]}','MNQ',own[boundary],own['available_at'])
            targets.append(dict(price=own[boundary],id=rid,kind='level',reason='Nearest still-untouched opposing HTF gap boundary'))
    for tf in p.timeframe_priority:
        bars=series['NQ',tf]
        if len(bars)<max(p.strength_lookback+1,4) or bars[-1]['available_at']!=cutoff:continue
        trigger=bars[-1];prior=bars[-p.strength_lookback-1:-1]
        if [b['time'] for b in prior+[trigger]]!=list(range(prior[0]['time'],cutoff,tf*60)):continue
        span=trigger['high']-trigger['low'];baseline=median(b['high']-b['low'] for b in prior)
        strength=dict(body_fraction=sign*(trigger['close']-trigger['open'])/span if span else 0,
                      close_location=(trigger['high']-trigger['close'] if short else trigger['close']-trigger['low'])/span if span else 0,
                      range_expansion=span/baseline if baseline else 0)
        strong=(strength['body_fraction']>=p.body_fraction and strength['close_location']>=p.close_location and baseline>0 and strength['range_expansion']>=p.expansion_ratio)
        for i in range(2,len(bars)-1):
            a,b,c=bars[i-2:i+1]
            if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60 or (c['low']<=a['high'] if short else c['high']>=a['low']):continue
            boundary=a['high'] if short else a['low']
            if cutoff-c['available_at']>p.gap_age_minutes*60:continue
            path=bars[i+1:]
            if [q['time'] for q in path]!=list(range(c['available_at'],cutoff,tf*60)):continue
            if sign*(trigger['close']-boundary)<=0 or any(sign*(q['close']-boundary)>0 for q in path[:-1]):continue
            for poi in pois:
                event=poi['extreme']
                # The gap must form during the same excursion (not an old gap
                # unrelated to today's sweep). Trigger cannot precede the POI.
                if not a['time']<=event['time']<=trigger['time']:continue
                recent=complete(nq,max(start,trigger['time']-30*60),trigger['time'])
                if not recent:continue
                rlo=min(x['low'] for x in recent);rhi=max(x['high'] for x in recent)
                origin=(rhi-c['high'] if short else c['low']-rlo)/(rhi-rlo) if rhi>rlo else 1
                faults=[]
                if not strong:faults.append('WEAK_CONFIRMATION_PROXY')
                if origin>p.extreme_fraction:faults.append('INVERSION_NOT_NEAR_RANGE_HIGH' if short else 'INVERSION_NOT_NEAR_RANGE_LOW')
                swings=[x for x in pivots(bars,favorable) if x['available_at']<=trigger['time'] and event['time']-30*60<=x['time']<=event['time']]
                swing=max(swings,key=lambda x:x['time']) if swings else None
                if not swing or sign*(trigger['close']-swing['price'])<=0:faults.append('SELECTED_LOW_NOT_BROKEN_ON_CLOSE' if short else 'SELECTED_HIGH_NOT_BROKEN_ON_CLOSE')
                # Initial stop is outside the entire paired MNQ reaction window,
                # not a post-entry trail or a copied NQ price.
                reaction=complete(mnq,event['time'],cutoff)
                anchor=min(reaction,key=lambda x:(sign*x[adverse],x['time']))
                stop=anchor[adverse]-sign*.25*p.stop_buffer_ticks
                target=min(targets,key=lambda x:(sign*x['price'],x['id'])) if targets else None
                if not target and swing:
                    own=next((x for x in mnq if x['time']==swing['time']),None)
                    # Use the complete matching TF candle for a TF swing.
                    own=next((x for x in series['MNQ',tf] if x['time']==swing['time']),None)
                    extreme=min(reaction,key=lambda x:(sign*x[adverse],x['time']))
                    if own and own['available_at']<=extreme['available_at']:
                        value=own[favorable]+p.projection_ratio*(own[favorable]-extreme[adverse])
                        if valid_price(value) and sign*(value-entry)>0:
                            rid=level(f'MNQ:PROJECTION:{tf}:{own["time"]}:{extreme["time"]}','MNQ',value,cutoff)
                            target=dict(price=value,id=rid,kind='level',reason='False-move projection fallback; fixed research ratio',projection=dict(ratio=p.projection_ratio,origin=own[favorable],extreme=extreme[adverse]))
                if not target:faults.append('NO_UNTOUCHED_TARGET_OR_VALID_PROJECTION')
                if not sign*(entry-stop)>0:faults.append('INVALID_STRUCTURAL_STOP')
                qty=min(max_contracts,int(risk_budget_usd/(sign*(entry-stop)*2))) if sign*(entry-stop)>0 else 0
                if qty<1:faults.append('RISK_BUDGET_TOO_SMALL')
                audit=dict(direction=direction,timeframe=tf,poi=poi['name'],recipe=poi['kind'],poi_rank=poi['rank'],poi_reference=poi['ref'],
                           strength=strength,range_origin=origin,smt=poi['smt'],stop_anchor_time=anchor['time'],stop=stop,
                           target=target,quantity=qty,reasons=faults,trigger_time=trigger['time'],gap_time=c['time'])
                result['candidates'].append(audit)
                if faults:continue
                swingid=add(f'NQ:AUTO_SWING:{tf}:{swing["time"]}',dict(kind='pivot',symbol='NQ',side=favorable,**swing))
                selection=dict(direction=direction,execution_symbol='MNQ',analysis_symbol='NQ',selected_at=now,timeframe_minutes=tf,
                    gap_bar_ids=[f'NQ:M{tf}:{x["time"]}' for x in (a,b,c)],trigger_bar_id=f'NQ:M{tf}:{trigger["time"]}',
                    requires_smt=poi['requires_smt'],requires_swing_break=True,requires_followthrough=False,swing_id=swingid,swing_break_mode='close',
                    entry_mode='immediate_after_confirmation',entry_mode_reason=poi['kind']+f' plus closed strong-inversion proxy and {favorable} break',
                    initial_stop=dict(mode='selected_structural_anchor',anchors=[dict(evidence_id=f'MNQ:M1:{anchor["time"]}',field=adverse)],buffer_ticks=p.stop_buffer_ticks,reason=f'Outermost MNQ {adverse} during selected reaction; causal structural-invalidation proxy'),
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
    selection.update(id=f'{VERSION}:{direction}:{cutoff}:{chosen["timeframe"]}:{fingerprint(asdict(p))[:12]}',packet_id=packet['packet_id'],clock_id=packet['clock_id'])
    observations=[]
    for f in (('bearish_thesis_supported','poi_reached','buyside_event_observed','nasdaq_bearish_confirmation','target_identified','selected_smt_present') if short else ('bullish_thesis_supported','poi_reached','sellside_event_observed','nasdaq_bullish_confirmation','target_identified','selected_smt_present')):
        observations.append(dict(field=f,value=chosen['smt'] if f=='selected_smt_present' else True,available_at=cutoff,clock_id=packet['clock_id'],packet_id=packet['packet_id'],source=VERSION+' computed proxy',evidence_ids=[chosen['poi_reference'],selection['trigger_bar_id']]))
    compiled=compile_entry(packet,selection,observations,now=now)
    for item in result['candidates']:item.pop('_selection',None)
    result.update(state='PLAN_READY' if compiled['plan'] else 'COMPILER_BLOCKED',selected=dict(audit=chosen,selection=selection,observations=observations),compiled=compiled,packet=packet)
    return result


def select(markets, *, cutoff, now, risk_budget_usd, max_contracts, policy=None):
    """Evaluate each direction on original prices. Conflicting plans abstain."""
    results=[_select_direction(markets,cutoff=cutoff,now=now,risk_budget_usd=risk_budget_usd,
              max_contracts=max_contracts,policy=policy,direction=d) for d in ('long','short')]
    ready=[r for r in results if r['state']=='PLAN_READY']
    result=dict(ready[0] if len(ready)==1 else results[0])
    result['direction_checks']={r['direction']:dict(state=r['state'],reasons=r['reasons']) for r in results}
    result['candidates']=[c for r in results for c in r['candidates']]
    if len(ready)>1:
        result.update(state='ABSTAIN',reasons=['CONFLICTING_DIRECTIONAL_PLANS'],selected=None)
        result.pop('compiled',None);result.pop('packet',None)
    elif not ready and results[0]['reasons']!=results[1]['reasons']:
        result['reasons']=list(dict.fromkeys(results[0]['reasons']+results[1]['reasons']))
    return result
