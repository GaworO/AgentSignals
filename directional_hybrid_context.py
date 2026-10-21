"""Causal, mechanical ICT observations. Never selects or submits a trade.

Confirmed pivots use two closed bars on either side. 'Liquidity' is an OHLC
hypothesis, not an order book. Mechanical range/SMT definitions are explicit;
none claims discretionary ICT confirmation. All values retain dependency IDs.
"""
import hashlib
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = 'directional-hybrid-v1'
FIELDS = ('market_thesis', 'liquidity_objective', 'entry_quality',
          'structural_invalidation', 'nearest_opposing_liquidity',
          'rr_assessment', 'conditional_management_plan')


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def build(packet, setup=None):
    cutoff = packet['as_of']; evidence = packet['evidence']
    # Validate even when used outside the worker. A caller cannot make future
    # evidence causal by changing only the packet cutoff.
    for key, e in evidence.items():
        at = e.get('available_at')
        if type(at) not in (int, float) or not math.isfinite(at) or at > cutoff:
            raise ValueError('hybrid_future_or_missing_timestamp')
        if e.get('kind') == 'bar':
            if e['time'] + e['timeframe'] * 60 != at:
                raise ValueError('hybrid_unclosed_bar')
            if not all(type(e[k]) in (int, float) and math.isfinite(e[k]) for k in ('open','high','low','close')):
                raise ValueError('hybrid_invalid_bar')
            if not 0<e['low']<=min(e['open'],e['close'])<=max(e['open'],e['close'])<=e['high']:
                raise ValueError('hybrid_invalid_ohlc_geometry')
        for ref in e.get('evidence_ids', []):
            if ref not in evidence or evidence[ref]['available_at'] > at:
                raise ValueError('hybrid_future_dependency')
    def node(value, refs=(), missing=()):
        refs = list(dict.fromkeys(refs))
        return dict(status='UNKNOWN' if value is None else ('PARTIAL' if missing else 'AVAILABLE'),
                    value=value, as_of=cutoff,
                    available_at=max((evidence[r]['available_at'] for r in refs), default=None),
                    source=dict(packet_id=packet.get('source_packet_id',packet.get('packet_id')),
                                evidence_ids=refs), missing=list(missing))
    def bars(symbol, tf):
        return sorted(((k,e) for k,e in evidence.items() if e.get('kind')=='bar'
                       and e.get('symbol')==symbol and e.get('timeframe')==tf), key=lambda x:x[1]['time'])
    def contiguous(rows, tf):
        return all(b[1]['time']-a[1]['time']==tf*60 for a,b in zip(rows,rows[1:]))
    def pivots(rows, tf):
        result=[]
        for i in range(2,len(rows)-2):
            w=rows[i-2:i+3]
            if not contiguous(w,tf): continue
            for side in ('high','low'):
                p=rows[i][1][side]
                others=[e[side] for j,(_,e) in enumerate(w) if j!=2]
                if (p>max(others) if side=='high' else p<min(others)):
                    result.append(dict(side=side,price=p,pivot_time=rows[i][1]['time'],
                                       available_at=w[-1][1]['available_at'],refs=[k for k,_ in w]))
        return result
    def bias(ps):
        hi=[p for p in ps if p['side']=='high'][-2:];lo=[p for p in ps if p['side']=='low'][-2:]
        if len(hi)<2 or len(lo)<2:return None
        return ('UP' if hi[1]['price']>hi[0]['price'] and lo[1]['price']>lo[0]['price'] else
                'DOWN' if hi[1]['price']<hi[0]['price'] and lo[1]['price']<lo[0]['price'] else 'MIXED')
    structures={}; all_pivots={}; events=[]; liquidity=[]
    for symbol in ('NQ','MNQ','ES'):
        for tf in (1,5,15,60,240):
            rows=bars(symbol,tf); ps=pivots(rows,tf);all_pivots[symbol,tf]=ps
            b=bias(ps)
            selected=[p for side in ('high','low') for p in [x for x in ps if x['side']==side][-2:]]
            used=[r for p in selected for r in p['refs']]
            if tf in (60,240):
                close=rows[-1][1]['close'] if rows else None
                objectives=[dict(side=p['side'],price=p['price'],available_at=p['available_at'],status='OHLC_CANDIDATE_NOT_CONFIRMED_RESTING_LIQUIDITY') for p in selected if close is not None and (p['price']>close if p['side']=='high' else p['price']<close)]
                structures[f'{symbol}_H{tf//60}']=node(dict(direction=b or 'UNKNOWN',draw_candidates=objectives),used,['Two confirmed highs and lows required; directional structure UNKNOWN'] if b is None else ['Discretionary HTF draw selection UNKNOWN'])
            # First CLOSED break after confirmation. Bias must be known before
            # the event; a change from an opposite bias is an MSS hypothesis.
            for pivot in ps:
                eligible=[(k,e) for k,e in rows if e['time']>=pivot['available_at']]
                touch=next(((k,e) for k,e in eligible if (e['high']>pivot['price'] if pivot['side']=='high' else e['low']<pivot['price'])),None)
                if touch:
                    k,e=touch
                    reclaim=e['close']<pivot['price'] if pivot['side']=='high' else e['close']>pivot['price']
                    if reclaim and tf>=5:
                        events.append(dict(kind='SWEEP_RECLAIM',symbol=symbol,timeframe=tf,
                            direction='DOWN' if pivot['side']=='high' else 'UP',available_at=e['available_at'],
                            price=pivot['price'],evidence_ids=pivot['refs']+[k]))
                hit=next(((k,e) for k,e in eligible if (e['close']>pivot['price'] if pivot['side']=='high' else e['close']<pivot['price'])),None)
                if hit and tf>=5:
                    k,e=hit;direction='UP' if pivot['side']=='high' else 'DOWN'
                    prior=bias([p for p in ps if p['available_at']<=e['time']])
                    kind='MSS' if prior in ('UP','DOWN') and prior!=direction else 'BOS' if prior==direction else 'STRUCTURE_BREAK'
                    events.append(dict(kind=kind,symbol=symbol,timeframe=tf,direction=direction,
                        prior_bias=prior,available_at=e['available_at'],price=pivot['price'],evidence_ids=pivot['refs']+[k]))
            if symbol=='MNQ' and tf>=5:
                for p in ps:
                    later=[e for _,e in bars('MNQ',1) if e['time']>=p['available_at']]
                    # Incomplete coverage cannot certify that a historical level is unswept.
                    full=bool(later) and later[0]['time']==p['available_at'] and later[-1]['available_at']==cutoff and all(
                        z['time']-a['time']==60 for a,z in zip(later,later[1:]))
                    taken=any(e['high']>p['price'] if p['side']=='high' else e['low']<p['price'] for e in later)
                    if full and taken:
                        key,hit=next((k,e) for k,e in bars('MNQ',1) if e['time']>=p['available_at'] and
                                     (e['high']>p['price'] if p['side']=='high' else e['low']<p['price']))
                        events.append(dict(kind='OBSERVED_LIQUIDITY_TAKEN',symbol='MNQ',timeframe=tf,
                            direction='UP' if p['side']=='high' else 'DOWN',available_at=hit['available_at'],
                            price=p['price'],evidence_ids=p['refs']+[key]))
                    liquidity.append(dict(side='BUY_SIDE' if p['side']=='high' else 'SELL_SIDE',price=p['price'],
                        timeframe=tf,available_at=p['available_at'],evidence_ids=p['refs'],
                        state='TAKEN' if taken else 'UNTAKEN_OBSERVED' if full else 'UNKNOWN_COVERAGE'))
    rows=bars('MNQ',5); ps=all_pivots['MNQ',5]
    highs=[p for p in ps if p['side']=='high']; lows=[p for p in ps if p['side']=='low']
    last=bars('MNQ',1)[-1:] ; price=last[0][1]['close'] if last else None
    range_node=node(None,missing=['Confirmed M5 opposite pivots unavailable'])
    if highs and lows and highs[-1]['price']>lows[-1]['price'] and price is not None:
        h,l=highs[-1],lows[-1];eq=(h['price']+l['price'])/2
        range_node=node(dict(high=h['price'],low=l['price'],equilibrium=eq,
            location='ABOVE_RANGE' if price>h['price'] else 'BELOW_RANGE' if price<l['price'] else
                     'PREMIUM' if price>eq else 'DISCOUNT' if price<eq else 'EQUILIBRIUM',
            entry_location=('ABOVE_RANGE' if setup['entry_price']>h['price'] else 'BELOW_RANGE' if setup['entry_price']<l['price'] else 'PREMIUM' if setup['entry_price']>eq else 'DISCOUNT' if setup['entry_price']<eq else 'EQUILIBRIUM') if setup else None,
            convention='Latest confirmed M5 high/low; mechanical candidate, not trader-selected dealing range'),h['refs']+l['refs']+[last[0][0]])
    gaps=[]
    # NQ is the analysis instrument. Its levels must not be compared directly
    # with MNQ entry prices. MNQ gaps are calculated separately from its bars.
    for k,e in evidence.items():
        if e.get('symbol')=='NQ' and e.get('kind') in ('FVG','IFVG','HTF_FVG'):
            gaps.append(dict(evidence_ids=[k],symbol='NQ',kind=e['kind'],timeframe=e['timeframe'],lower=e['lower'],upper=e['upper'],
                available_at=e['available_at'],direction=e.get('direction',e.get('side')),
                entry_inside=None,lifecycle='SOURCE_EVENT; subsequent mitigation UNKNOWN'))
    for tf in (1,5,15,60,240):
        rs=bars('MNQ',tf)
        for i in range(2,len(rs)):
            window=rs[i-2:i+1]
            if not contiguous(window,tf):continue
            a,c=window[0][1],window[2][1]
            direction='bull' if c['low']>a['high'] else 'bear' if c['high']<a['low'] else None
            if direction is None:continue
            low,high=(a['high'],c['low']) if direction=='bull' else (c['high'],a['low'])
            refs=[k for k,_ in window];following=rs[i+1:]
            inverted=next(((k,e) for k,e in following if (e['close']<low if direction=='bull' else e['close']>high)),None)
            touched=any(e['low']<=high and e['high']>=low for _,e in following)
            complete=contiguous(rs[i:],tf)
            gaps.append(dict(evidence_ids=refs,symbol='MNQ',kind='FVG',timeframe=tf,lower=low,upper=high,
                direction=direction,available_at=c['available_at'],entry_inside=low<=setup['entry_price']<=high if setup else None,
                lifecycle='INVERTED' if inverted else 'TOUCHED' if touched else 'UNTOUCHED_OBSERVED' if complete else 'UNKNOWN_COVERAGE'))
            if inverted:
                k,e=inverted
                gaps.append(dict(evidence_ids=refs+[k],symbol='MNQ',kind='IFVG',timeframe=tf,lower=low,upper=high,
                    direction='bear' if direction=='bull' else 'bull',available_at=e['available_at'],
                    entry_inside=low<=setup['entry_price']<=high if setup else None,lifecycle='INVERSION_RECORDED'))
    # Bound the summary, not the original evidence. Prefer a nearby MNQ gap
    # for the candidate plus recent events; no cross-contract price translation.
    for gap in gaps:
        if gap['kind']=='IFVG' and gap['timeframe']>=5:
            up=gap['direction'] in ('bull','long')
            events.append(dict(kind='IFVG',symbol=gap['symbol'],timeframe=gap['timeframe'],
                direction='UP' if up else 'DOWN',available_at=gap['available_at'],
                price=gap['upper'] if up else gap['lower'],evidence_ids=gap['evidence_ids']))
    gaps.sort(key=lambda g:g['available_at'],reverse=True)
    if setup:
        near=sorted((g for g in gaps if g['symbol']=='MNQ'),key=lambda g:min(abs(g['lower']-setup['entry_price']),abs(g['upper']-setup['entry_price'])))[:4]
    else:near=[]
    latest={}
    for g in gaps:latest.setdefault((g['symbol'],g['timeframe'],g['kind']),g)
    selected=near+list(latest.values())+gaps[:8]
    gaps=list({sha(g):g for g in selected}.values())
    displacement=[]
    for tf in (1,5,15):
        rs=bars('MNQ',tf)
        if len(rs)>=16 and contiguous(rs[-16:],tf):
            trs=[max(e['high']-e['low'],abs(e['high']-rs[i-1][1]['close']),abs(e['low']-rs[i-1][1]['close']))
                 for i,(_,e) in enumerate(rs) if i>0]
            atr=sum(trs[-15:-1])/14; k,e=rs[-1]; body=abs(e['close']-e['open']);span=e['high']-e['low']
            displacement.append(dict(timeframe=tf,prior_atr14=atr,body_fraction=body/span if span else 0,
                range_atr=span/atr if atr else None,displacement=bool(atr and body>=atr and span and body/span>=.6),
                regime='EXPANSION' if atr and span>=1.5*atr else 'CONSOLIDATION' if atr and span<=.5*atr else 'NORMAL',
                available_at=e['available_at'],evidence_ids=[k for k,_ in rs[-16:]]))
    align=node(None,missing=['30 aligned contiguous M1 observations required']); smt=node(None,missing=['Paired confirmed ES/NQ swings at identical times unavailable'])
    es=bars('ES',1)[-30:];nq=bars('NQ',1)[-30:]
    if len(es)==len(nq)==30 and contiguous(es,1) and contiguous(nq,1) and [e['time'] for _,e in es]==[e['time'] for _,e in nq]:
        a=es[-1][1]['close']-es[0][1]['open'];b=nq[-1][1]['close']-nq[0][1]['open']
        align=node(dict(es_change=a,nq_change=b,aligned=a*b>0,convention='30 closed M1 bars; alignment is not SMT'),[k for k,_ in es+nq])
    smts=[]
    for side in ('high','low'):
        ep={p['pivot_time']:p for p in all_pivots['ES',5] if p['side']==side}
        np={p['pivot_time']:p for p in all_pivots['NQ',5] if p['side']==side}
        times=sorted(ep.keys()&np.keys())[-2:]
        if len(times)==2:
            t0,t1=times; da=ep[t1]['price']-ep[t0]['price'];db=np[t1]['price']-np[t0]['price']
            smts.append(dict(side=side,divergence=((da>0)!=(db>0)) if side=='high' else ((da<0)!=(db<0)),es_change=da,nq_change=db,
                paired_pivot_times=times,evidence_ids=sum((ep[t]['refs']+np[t]['refs'] for t in times),[])))
    if smts:smt=node(smts,[r for s in smts for r in s['evidence_ids']],['Mechanical synchronized swing comparison; discretionary anchor selection UNKNOWN'])
    for e in events:e['id']=sha(e)
    events=sorted(events,key=lambda e:(e['available_at'],e['id']))
    # Retain the recent hour plus the last event of each kind/timeframe/market.
    # Original bar history remains in packet.evidence; no future data is used.
    latest={(e['symbol'],e['timeframe'],e['kind']):e['id'] for e in events}
    events=[e for e in events if e['available_at']>=cutoff-3600 or e['id'] in latest.values()]
    entry=setup['entry_price'] if setup else price
    opposing=[] if entry is None else sorted((x for x in liquidity if x['state']!='TAKEN' and
        ((x['price']>entry and x['side']=='BUY_SIDE') if not setup or setup['direction']=='LONG' else
         (x['price']<entry and x['side']=='SELL_SIDE'))),key=lambda x:abs(x['price']-entry))
    nearest=opposing[0] if opposing else None
    geometry=None
    if setup:
        sign=1 if setup['direction']=='LONG' else -1;r=sign*(entry-setup['stop_price']);reward=sign*(setup['target_price']-entry)
        geometry=dict(source=dict(order_id=setup['order_id'],path='setup'),available_at=setup['activation_ms']/1000,order_id=setup['order_id'],direction=setup['direction'],entry=entry,stop=setup['stop_price'],target=setup['target_price'],
            expiry_ms=setup['expiry_ms'],gross_rr=reward/r,net_rr=None,net_rr_status='UNKNOWN_VERIFIED_COSTS_AND_EXECUTABLE_QUOTE_REQUIRED',
            target_distance_points=reward,remaining_from_observed_close=sign*(setup['target_price']-price) if price else None,
            nearest_opposing_distance=abs(nearest['price']-entry) if nearest else None,
            opposing_before_target=abs(nearest['price']-entry)<reward if nearest else None)
    daily_atr=node(None,missing=['Causal daily ATR unavailable'])
    av=packet.get('daily_atr')
    if isinstance(av,dict) and type(av.get('available_at')) in (int,float) and 0<av['available_at']<=cutoff:
        daily_atr=node(av,missing=['Preserve source completeness flags; ATR is not ICT confirmation'])
        daily_atr['available_at']=av['available_at'];daily_atr['source']['path']='daily_atr'
    return dict(version=VERSION,as_of=cutoff,source_packet_id=packet.get('source_packet_id',packet.get('packet_id')),
        timestamp_semantics='Bar close / confirmation availability; individual receipt timestamps UNKNOWN unless source supplies them',
        structure_event_scope='Last 60 minutes plus latest per symbol/timeframe/kind; full source bars retained',
        htf_structure=structures,liquidity=node(liquidity,[r for x in liquidity for r in x['evidence_ids']],['OHLC liquidity hypotheses; coverage state must be checked']),
        draw_on_liquidity=node(nearest,nearest['evidence_ids'] if nearest else [],['Nearest mechanical candidate; discretionary HTF narrative UNKNOWN']),
        dealing_range=range_node,structure_events=node(events,[r for e in events for r in e['evidence_ids']]),
        fvg_ifvg=node(gaps,[r for g in gaps for r in g['evidence_ids']],['Summary: latest per market/timeframe/kind plus recent and near-entry MNQ gaps; full NQ source events retained']) if gaps else node(None,missing=['Gap evidence unavailable']),
        displacement_atr=node(displacement,[r for x in displacement for r in x['evidence_ids']]) if displacement else node(None,missing=['16 contiguous bars required']),
        daily_atr=daily_atr,
        es_nq_alignment=align,smt=smt,geometry=geometry,
        ny_time=datetime.fromtimestamp(cutoff,ZoneInfo('America/New_York')).isoformat())


def budget_reason(rows, external, kind, cap=12):
    """Fixed 3/7/2 policy for Phase 2A. No transfers; position reserve unspent.

    Every persisted attempt counts, even crashes/timeouts. Legacy/external
    usage occupies capacity conservatively in addition to class reservations.
    All calls occur in NYAM/NYPM, so NY calendar and futures trade date agree.
    """
    if len(rows)+external>=min(cap,12):return 'DAILY_LIMIT_REACHED'
    counts={k:sum(r['review_kind']==k for r in rows) for k in ('CONTEXT','SETUP','POSITION')}
    if kind not in ('CONTEXT','SETUP'):return 'POSITION_REVIEW_NOT_IMPLEMENTED'
    if counts[kind]>={'CONTEXT':3,'SETUP':7}[kind]:return kind+'_ALLOCATION_EXHAUSTED'
    reserved=2+(max(0,7-counts['SETUP']) if kind=='CONTEXT' else 0)
    if len(rows)+external+1+reserved>min(cap,12):return 'RESERVED_CAPACITY_PROTECTED'
    return None


def schema(base, setup):
    import copy
    value=copy.deepcopy(base)
    string={'type':'string'}; ids={'type':'array','items':string}
    field=dict(type='object',properties=dict(assessment=string,evidence_ids=ids,missing=ids),
               required=['assessment','evidence_ids','missing'],additionalProperties=False)
    value['properties']['hybrid_assessment']=dict(type='object',properties={k:field for k in FIELDS},required=list(FIELDS),additionalProperties=False)
    value['required'].append('hybrid_assessment')
    if setup:
        fields={k:dict(type='number') for k in ('entry_price','stop_price','target_price','expiry_ms')}
        value['properties']['approved_geometry']=dict(type='object',properties=fields,required=list(fields),additionalProperties=False)
        value['required'].append('approved_geometry')
    return value


def validate(decision, packet):
    for field in decision['hybrid_assessment'].values():
        if not field['assessment'].strip() or any(r not in packet['evidence'] for r in field['evidence_ids']):
            raise ValueError('hybrid_reason_or_reference_invalid')
        if not field['evidence_ids'] and not field['missing']:
            raise ValueError('hybrid_unsubstantiated_assessment')
    if packet.get('setup'):
        if decision['approved_geometry']!={k:packet['setup'][k] for k in ('entry_price','stop_price','target_price','expiry_ms')}:
            raise ValueError('hybrid_geometry_mismatch')
