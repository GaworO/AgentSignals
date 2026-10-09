"""Compile explicitly selected Tanja directional-entry evidence into a research plan.

This calculates confirmation and prices; it does NOT discover a discretionary
POI, decide that a candle is 'strong', or choose the trader's preferred target.
No broker transport. No transcript text is used as a same-session market input.
"""
import hashlib
import json
import math
from context_filter import review_context
from plan_review import review_plan


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def finite(x):
    return type(x) in (int,float) and math.isfinite(x)


def tick(x):
    return finite(x) and x>0 and abs(x*4-round(x*4))<1e-8


def compile_entry(packet, selection, observations, *, now):
    """Observations are explicit context annotations, not inferred market facts.

    packet: market-only evidence frozen at as_of, with canonical packet_id.
    selection: immutable choices made no earlier than that cutoff.
    Bar start/time and available_at use UTC seconds; timeframe is minutes.
    """
    result=dict(state='NEEDS_SELECTION',plan=None,context=None,price_review=None,
                errors=[],missing=[],measurements={},orders_enabled=False,
                automatic_context_selection=False,semantic_fidelity_verified=False)
    missing=result['missing'];errors=result['errors'];m=result['measurements']
    try:
        if type(now) is not int:raise ValueError('INVALID_NOW')
        if packet.get('labels_included') is not False or packet.get('input_mode')!='market_only':
            raise ValueError('MARKET_ONLY_PACKET_REQUIRED')
        if fingerprint({k:v for k,v in packet.items() if k!='packet_id'})!=packet['packet_id']:
            raise ValueError('PACKET_HASH_MISMATCH')
        cutoff=packet['as_of'];selected=selection.get('selected_at')
        if type(cutoff) is not int or type(selected) is not int or not 0<=cutoff<=selected<=now:
            raise ValueError('NONCAUSAL_SELECTION_TIME')
        if selection.get('packet_id')!=packet['packet_id']:raise ValueError('WRONG_SELECTION_PACKET')
        if selection.get('clock_id')!=packet.get('clock_id') or not str(packet.get('clock_id','')).endswith(':unix_utc'):
            raise ValueError('MARKET_CLOCK_REQUIRED')
        direction=selection.get('direction');sign=-1 if direction=='short' else 1
        short=direction=='short'
        swing_side='low' if short else 'high'
        swing_field='selected_swing_low_broken' if short else 'selected_swing_high_broken'
        if direction not in ('long','short') or selection.get('execution_symbol')!='MNQ':
            raise ValueError('UNSUPPORTED_LIFECYCLE')
        analysis=selection.get('analysis_symbol')
        if analysis not in ('NQ','MNQ'):raise ValueError('UNSUPPORTED_ANALYSIS_SYMBOL')
        result['analysis_source_matches_tanja']=analysis=='NQ'
        ev=packet['evidence']
        for item in ev.values():
            at=item.get('available_at')
            if type(at) is not int or not 0<=at<=cutoff:raise ValueError('FUTURE_OR_UNDATED_EVIDENCE')
        def evidence(ref, symbol=None):
            if ref not in ev:raise ValueError('MISSING_EVIDENCE_REFERENCE')
            item=ev[ref]
            if symbol and item.get('symbol')!=symbol:raise ValueError('CROSS_SERIES_ANCHOR')
            return item
        def point(ref, symbol='MNQ'):
            item=evidence(ref['evidence_id'],symbol);field=ref['field']
            allowed={'bar':{'high','low','open','close'},'pivot':{'price'},'level':{'price'},'FVG':{'lower','upper'},'HTF_FVG':{'lower','upper'},'IFVG':{'lower','upper'}}
            if field not in allowed.get(item['kind'],set()) or not tick(item.get(field)):
                raise ValueError('INVALID_PRICE_ANCHOR')
            if item['kind']=='level' and item.get('label')=='ATH' and item.get('history_verified') is not True:
                raise ValueError('ATH_HISTORY_UNVERIFIED')
            return dict(symbol=symbol,price=item[field],available_at=item['available_at'],source=ref['evidence_id']+':'+field)
        def bar(ref, tf):
            b=evidence(ref,analysis)
            if b.get('kind')!='bar' or b.get('timeframe')!=tf:raise ValueError('WRONG_CONFIRMATION_TIMEFRAME')
            if type(b.get('time')) is not int or b['time']+60*tf!=b['available_at']:
                raise ValueError('UNCLOSED_CONFIRMATION')
            if not all(tick(b.get(k)) for k in ('open','high','low','close')) or not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:
                raise ValueError('INVALID_BAR')
            return b

        # Calculate the chosen gap and opposite-direction inversion from bars, not
        # a model's assertion that a lower-timeframe wick counts as confirmation.
        tf=selection.get('timeframe_minutes')
        if type(tf) is not int or tf not in (1,2,3,5):raise ValueError('UNSUPPORTED_TIMEFRAME')
        refs=selection.get('gap_bar_ids')
        if not isinstance(refs,list) or len(refs)!=3 or len(set(refs))!=3:raise ValueError('THREE_GAP_BARS_REQUIRED')
        a,b,c=[bar(r,tf) for r in refs];trigger=bar(selection['trigger_bar_id'],tf)
        if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60 or c['available_at']>trigger['time']:
            raise ValueError('GAP_BAR_SEQUENCE_INVALID')
        if (c['low']<=a['high'] if short else c['high']>=a['low']):raise ValueError('DIRECTIONAL_GAP_NOT_PRESENT')
        boundary=a['high'] if short else a['low']
        path=sorted([x for x in ev.values() if x.get('kind')=='bar' and x.get('symbol')==analysis and x.get('timeframe')==tf and c['available_at']<=x.get('time',-1)<=trigger['time']],key=lambda x:x['time'])
        if [x['time'] for x in path]!=list(range(c['available_at'],trigger['time']+1,tf*60)):
            raise ValueError('INCOMPLETE_GAP_TO_TRIGGER_HISTORY')
        for row in path:
            if not all(tick(row.get(k)) for k in ('open','high','low','close')) or row['available_at']!=row['time']+tf*60 or not row['low']<=min(row['open'],row['close'])<=max(row['open'],row['close'])<=row['high']:
                raise ValueError('INVALID_INTERVENING_BAR')
        if any(sign*(x['close']-boundary)>0 for x in path[:-1]):raise ValueError('TRIGGER_IS_NOT_FIRST_INVERSION')
        m.update(gap_lower=a['high'] if short else c['high'],gap_upper=c['low'] if short else a['low'],inversion_closed=sign*(trigger['close']-boundary)>0,trigger_available_at=trigger['available_at'])
        if trigger['available_at']!=cutoff:raise ValueError('FRESH_TRIGGER_PACKET_REQUIRED')
        if type(selection.get('requires_swing_break')) is bool and selection['requires_swing_break']:
            swing=evidence(selection['swing_id'],analysis)
            if swing.get('kind')!='pivot' or swing.get('side')!=swing_side or swing['available_at']>trigger['time'] or not tick(swing.get('price')):
                raise ValueError('SWING_NOT_KNOWN_BEFORE_TRIGGER')
            mode=selection.get('swing_break_mode')
            if mode not in ('wick','close'):raise ValueError('SWING_BREAK_MODE_REQUIRED')
            m['swing_broken']=sign*(trigger[swing_side if mode=='wick' else 'close']-swing['price'])>0

        recipe=dict(kind='momentum_'+direction,timeframe_minutes=tf,**{k:selection.get(k) for k in ('requires_smt','requires_swing_break','requires_followthrough')})
        derived_fields={'selected_inversion_closed','selected_swing_high_broken','selected_swing_low_broken'}
        supplied=[]
        for o in observations:
            if o.get('packet_id')!=packet['packet_id'] or type(o.get('available_at')) is not int or o['available_at']>cutoff:
                raise ValueError('CONTEXT_NOT_FROM_FROZEN_PACKET')
            if o.get('field') in derived_fields:raise ValueError('MODEL_CANNOT_OVERRIDE_MEASURED_CONFIRMATION')
            if not o.get('evidence_ids') or any(ref not in ev for ref in o['evidence_ids']):
                raise ValueError('UNREFERENCED_CONTEXT_OBSERVATION')
            supplied.append(o)
        for field,value in [('selected_inversion_closed',m['inversion_closed']),(swing_field,m.get('swing_broken'))]:
            if value is not None:
                supplied.append(dict(field=field,value=value,available_at=cutoff,clock_id=packet['clock_id'],packet_id=packet['packet_id'],source=selection['trigger_bar_id'],timeframe_minutes=tf))
        context=review_context(recipe,supplied,as_of=cutoff,clock_id=packet['clock_id']);result['context']=context
        result['recipe']=recipe;result['review_observations']=supplied
        mode=selection.get('entry_mode')
        if mode not in ('immediate_after_confirmation','retracement'):missing.append('entry_mode')
        justification=selection.get('entry_mode_reason')
        if not isinstance(justification,str) or not justification.strip():missing.append('entry_mode_reason')

        stop_spec=selection.get('initial_stop')
        invalidation=None;buffer=None
        if not isinstance(stop_spec,dict):missing.append('initial_stop_selection')
        else:
            anchors=stop_spec.get('anchors',[])
            if stop_spec.get('mode') not in ('selected_structural_anchor','above_all_selected_resistances' if short else 'below_all_selected_supports'):
                missing.append('initial_stop_mode')
            elif not anchors or (stop_spec['mode']=='selected_structural_anchor' and len(anchors)!=1):
                missing.append('initial_stop_anchors')
            else:
                pts=[point(x) for x in anchors]
                # min is the consequence of the explicitly chosen 'below all'
                # contract, not an automatic choice of the trader's supports.
                invalidation=min(pts,key=lambda x:sign*x['price'])
                m['invalidation_sources']=[p['source'] for p in pts]
            buffer=stop_spec.get('buffer_ticks')
            if buffer is None:missing.append('initial_stop_buffer_ticks')
            if not stop_spec.get('reason'):missing.append('initial_stop_reason')

        target_spec=selection.get('target');target=None
        if not isinstance(target_spec,dict):missing.append('target_selection')
        elif target_spec.get('kind')=='level':
            target=dict(kind='level',anchor=point(target_spec['anchor']))
        elif target_spec.get('kind') in ('extension_minus_half','extension_minus_one'):
            origin=point(target_spec['anchor']);extreme=point(target_spec['extreme'])
            if origin['available_at']>extreme['available_at'] or sign*(origin['price']-extreme['price'])<=0:
                raise ValueError('INVALID_FALSE_MOVE_SEQUENCE')
            ratio=.5 if target_spec['kind']=='extension_minus_half' else 1
            raw=origin['price']+ratio*(origin['price']-extreme['price'])
            m['projection_raw']=raw
            # Do not silently round or infer a front-running offset from one fill.
            if not tick(raw):raise ValueError('OFF_TICK_PROJECTION_REQUIRES_EXPLICIT_POLICY')
            target=dict(kind='level',anchor=dict(symbol='MNQ',price=raw,available_at=max(origin['available_at'],extreme['available_at']),source=target_spec['kind']+':'+origin['source']+':'+extreme['source']))
        else:missing.append('target_kind')
        if target_spec and not target_spec.get('reason'):missing.append('target_reason')
        entry=point(selection['entry_reference']) if selection.get('entry_reference') else None
        plan=dict(plan_id=selection.get('id'),packet_id=packet['packet_id'],symbol='MNQ',direction=direction,selected_at=selected,
                  context_valid_until=selection.get('context_valid_until'),context_supported=context['state']=='REVIEW_CANDIDATE',
                  trigger_available_at=trigger['available_at'],entry_mode=mode,entry_reference=entry,invalidation=invalidation,
                  stop_buffer_ticks=buffer,target=target,initial_quantity=selection.get('initial_quantity'),risk_budget_usd=selection.get('risk_budget_usd'))
        if mode=='retracement':
            plan.update(entry_valid_until=selection.get('entry_valid_until'),cancel_condition=selection.get('cancel_condition'))
        reviewed=review_plan(plan,now=now);result['price_review']=reviewed
        missing.extend(reviewed['missing']);errors.extend(reviewed['errors'])
        if context['state']!='REVIEW_CANDIDATE':result['state']='WAIT_FOR_CONTEXT'
        elif missing:result['state']='NEEDS_SELECTION'
        elif errors:result['state']='REJECTED'
        elif mode=='retracement':result['state']='RETRACEMENT_EXECUTOR_NOT_IMPLEMENTED'
        else:
            result['state']='HYPOTHETICAL_PLAN_COMPLETE';result['plan']=plan
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        result['state']='REJECTED';errors.append(str(exc))
    result['missing']=sorted(set(missing));result['errors']=sorted(set(errors))
    return result
