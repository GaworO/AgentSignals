"""Reusable saved-context rules for NEW inversions; no additional AI calls.

Context judgments/price priorities stay frozen. New bars supply only the
explicit sequence, inversion, stop-rule and optional limit-rule measurements.
Every candidate receives a result, including opposite or incomplete setups.
"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import math
from context_v3 import (object_schema,nullable,enum,array,STR,point_price,check_packet,check_schema,build_v3_packet)
from tanya_replay import Bar
from entry_rules import fingerprint
from entry_patterns import PROFILES
from source_rules import PROFILES as ALL_PROFILES

VERSION='saved-context-two-family-scanner-v2'
NY=ZoneInfo('America/New_York')


def schema():
    point=object_schema(dict(evidence_id=STR,field=enum(['high','low','open','close','price','lower','upper'])))
    local=object_schema(dict(anchor=point,symbol=enum(['ES','MNQ']),reason=STR))
    sequence=object_schema(dict(events_since={'type':'integer'},manipulation_level=nullable(point),
        manipulation_side=nullable(enum(['above','below'])),structure_swing_id={'type':['string','null']},
        structure_role=nullable(enum(['liquidity_sweep','directional_break'])),structure_break_mode=nullable(enum(['wick','close'])),
        retracement_zone_id={'type':['string','null']},htf_objective=nullable(point)))
    smt=object_schema(dict(reference_es=STR,reference_mnq=STR,current_window_minutes={'type':'integer'}))
    return nullable(object_schema(dict(armed={'type':'boolean'},setup_profile=enum(list(ALL_PROFILES)),
        timeframes=array(enum(['1','2','3','5'])),valid_for_seconds={'type':'integer'},
        sequence_rule=nullable(sequence),sequence_selector=enum(['selected_anchors','two_family_v1']),entry_mode=enum(['immediate_after_confirmation','retracement']),
        entry_limit_rule=enum(['inverted_gap_edge','confirmation_open','selected_anchor']),entry_anchor=nullable(point),
        stop_rule=enum(['selected_anchor','setup_extreme','retracement_extreme','confirmation_extreme']),stop_anchor=nullable(point),
        stop_buffer_ticks={'type':'integer'},thesis_invalidation=point,final_target=point,local_objective=nullable(local),
        trade_kind=enum(['continuation','reversal']),requires_smt={'type':'boolean'},smt_rule=nullable(smt),
        directional_swing_id={'type':['string','null']},directional_swing_mode=enum(['wick','close']),
        poi_evidence_ids=array(STR),reason=STR)))


PROMPT='''
Reusable context watch extension:
entry_watch is a bounded plan for future confirmations, distinct from entry_setup
which selects an already observed inversion. You can arm a watch while context
decision=wait ONLY when waiting for its trigger, higher-timeframe context and
structure are accepted, direction is long/short, and missing_context is empty.
If context itself is uncertain, abstain/wait with entry_watch=null or armed=false.
No future evidence IDs or fabricated strong-candle claims. Choose the family,
known POI/levels, price priorities and explicit stop/limit rules. The mechanical
future confirmation is a strict inversion close plus the chosen causal sequence;
the software does not re-infer your discretionary quality judgment.
timeframes are strings "1","2","3","5"; new sequence families use ["1"].
Lifetime is 60–900 seconds from THIS review cutoff. New reviews supersede watches.
sequence_rule.events_since selects an already-known search start. For manipulation
select the known level, manipulation side, swing, its role/break mode and zone.
For HTF select an H1/H4 objective; retracement_zone_id can select an existing M15
gap, or null to permit the first directional M15 gap after that objective touch.
Unused sequence fields are null. Latest qualified zone retest before confirmation
is selected; a new candidate cannot reuse a previously inverted gap as a new event.
stop_rule=selected_anchor uses the cited point; setup_extreme uses the most adverse
selected sequence/formation/confirmation candle; retracement_extreme uses the
selected pre-confirmation retracement; confirmation_extreme uses the trigger.
Supply an explicit buffer in 0.25-point ticks; these are chosen rules, not universal
Tanja defaults. entry_limit_rule=inverted_gap_edge selects upper for a long, lower
for a short; confirmation_open selects the trigger open. selected_anchor requires
entry_anchor. Any retracement limit must improve on the confirmation close.
final_target is one fixed selected MNQ objective. local_objective is optional.
SMT is optional. If required, select paired historical ES/selected-Nasdaq reference windows
and a current rolling window length in minutes; current windows are recomputed
on each new candidate, never copied from the earlier review.
Only arm a policy you can justify from the supplied evidence. List data/context
gaps instead of creating a convenient stop/target or assuming a session outcome.
sequence_selector=selected_anchors keeps your explicit sequence_rule anchors.
sequence_selector=two_family_v1 explicitly authorizes the shared May sequence
scanner for your chosen family and direction; use sequence_rule=null. It uses
strict two-left/two-right M1 pivots, a 180-minute sequence horizon, the latest
qualifying manipulation/structure event and its candle-range retest, or a low
(long)/high (short) of the last eight closed H1/four H4 candles, followed by a
directional M15 gap and its latest retest. Every event must be known before use.
This is a disclosed mechanical selector, not a discretionary Tanja judgment.
Choose retracement_extreme and an explicit buffer for the tested stop style;
optional further entry uses confirmation_open or an explicit MNQ anchor.
The software keeps YOUR fixed MNQ final/local targets, continuation/reversal
classification, SMT requirement, thesis invalidation and expiry. It does not
reuse the backtest's nearest-target or hourly-body classification proxies.
Packet setup_scanner candidates are measured sequences, never context approvals.
'''


def validate_watch(packet,decision,watch):
    s=schema()
    if 'sequence_selector' not in watch:
        s['anyOf'][0]['properties'].pop('sequence_selector');s['anyOf'][0]['required'].remove('sequence_selector')
    check_schema(watch,s)
    if not watch['armed']:return False
    context=decision['context']
    if (context['decision']=='abstain' or context['bias'] not in ('long','short') or context['missing_context']
        or any(context['claims'][k]['value'] is not True for k in ('higher_timeframe_context_valid','structure_intact'))):
        raise ValueError('WATCH_NEEDS_ACCEPTED_DIRECTIONAL_CONTEXT')
    analysis=decision['analysis_symbol']
    if analysis not in ('NQ','MNQ') or decision['execution_symbol']!='MNQ':raise ValueError('WATCH_NASDAQ_ANALYSIS_MNQ_EXECUTION_REQUIRED')
    if not watch['reason'].strip() or not 60<=watch['valid_for_seconds']<=900:raise ValueError('WATCH_REASON_AND_BOUNDED_LIFETIME_REQUIRED')
    tfs=[int(x) for x in watch['timeframes']]
    if not tfs or len(tfs)!=len(set(tfs)) or any(x not in ALL_PROFILES[watch['setup_profile']]['timeframes'] for x in tfs):raise ValueError('WATCH_PROFILE_TIMEFRAMES_INVALID')
    if watch['stop_buffer_ticks']<0:raise ValueError('WATCH_EXPLICIT_NONNEGATIVE_BUFFER_REQUIRED')
    for ref in watch['poi_evidence_ids']:
        if ref not in packet['evidence']:raise ValueError('WATCH_POI_REFERENCE_MISSING')
    if not watch['poi_evidence_ids']:raise ValueError('WATCH_POI_SELECTION_REQUIRED')
    point_price(packet,watch['thesis_invalidation'],analysis)
    point_price(packet,watch['final_target'],'MNQ')
    if analysis=='NQ' and watch['entry_mode']=='retracement' and watch['entry_limit_rule']=='inverted_gap_edge':raise ValueError('EXPLICIT_MNQ_RETRACEMENT_RULE_REQUIRED')
    if watch['stop_rule']=='selected_anchor':
        if watch['stop_anchor'] is None:raise ValueError('WATCH_STOP_ANCHOR_REQUIRED')
        point_price(packet,watch['stop_anchor'],'MNQ')
    if watch['entry_mode']=='retracement' and watch['entry_limit_rule']=='selected_anchor':
        if watch['entry_anchor'] is None:raise ValueError('WATCH_ENTRY_ANCHOR_REQUIRED')
        point_price(packet,watch['entry_anchor'],'MNQ')
    if watch['local_objective']:
        point_price(packet,watch['local_objective']['anchor'],watch['local_objective']['symbol'])
    sequence=watch['sequence_rule'];family=watch['setup_profile']
    discovery=watch.get('sequence_selector','selected_anchors')=='two_family_v1'
    if discovery and (analysis!='NQ' or family not in PROFILES or tfs!=[1] or sequence is not None):
        raise ValueError('SCANNER_REQUIRES_NQ_M1_FAMILY_AND_NULL_EXPLICIT_SEQUENCE')
    if family in PROFILES:
        if not discovery and (sequence is None or not 0<=sequence['events_since']<=packet['as_of']):raise ValueError('WATCH_CAUSAL_SEQUENCE_RULE_REQUIRED')
        if not discovery and family==PROFILES[0]:
            point_price(packet,sequence['manipulation_level'],analysis)
            swing=packet['evidence'].get(sequence['structure_swing_id'],{})
            expected=('high' if context['bias']=='long' else 'low') if sequence['structure_role']=='directional_break' else ('low' if context['bias']=='long' else 'high')
            if swing.get('kind')!='pivot' or swing.get('symbol')!=analysis or swing.get('side')!=expected:raise ValueError('WATCH_STRUCTURE_SWING_REQUIRED')
            if sequence['structure_role'] not in ('directional_break','liquidity_sweep') or sequence['structure_break_mode'] not in ('wick','close') or sequence['manipulation_side'] not in ('above','below'):raise ValueError('WATCH_STRUCTURE_RULE_REQUIRED')
            if sequence['retracement_zone_id'] not in packet['evidence']:raise ValueError('WATCH_RETRACEMENT_ZONE_REQUIRED')
        elif not discovery:
            point_price(packet,sequence['htf_objective'],analysis)
            if packet['evidence'][sequence['htf_objective']['evidence_id']].get('timeframe') not in (60,240):raise ValueError('WATCH_H1_H4_OBJECTIVE_REQUIRED')
            if sequence['retracement_zone_id'] is not None and sequence['retracement_zone_id'] not in packet['evidence']:raise ValueError('WATCH_M15_ZONE_REFERENCE_MISSING')
    if family=='m5_momentum' and (watch['directional_swing_id'] is None or watch['directional_swing_mode']!='close'):raise ValueError('WATCH_M5_SWING_CLOSE_REQUIRED')
    if watch['directional_swing_id'] is not None:
        swing=packet['evidence'].get(watch['directional_swing_id'],{})
        if swing.get('symbol')!=analysis or swing.get('kind')!='pivot' or swing.get('side')!=('high' if context['bias']=='long' else 'low'):raise ValueError('WATCH_DIRECTIONAL_SWING_INVALID')
    if watch['requires_smt']:
        s=watch['smt_rule']
        if s is None or not 1<=s['current_window_minutes']<=30:raise ValueError('WATCH_SMT_RULE_REQUIRED')
        windows=[packet['evidence'].get(s[k],{}) for k in ('reference_es','reference_mnq')]
        if any(w.get('kind')!='window' or w.get('complete') is not True for w in windows) or (windows[0]['symbol'],windows[1]['symbol'])!=('ES',analysis) or (windows[0]['start'],windows[0]['end'])!=(windows[1]['start'],windows[1]['end']):raise ValueError('WATCH_MATCHED_SMT_REFERENCE_WINDOWS_REQUIRED')
    return True


def derive_watch(packet,decision):
    """User-authorized narrow reuse of a completed setup's fixed context/levels.
    It does not infer new discretionary POIs, buffer or target priorities.
    """
    ext=decision.get('entry_setup');context=decision['context']
    if not ext or context['decision']!='candidate' or not decision['initial_stop'] or len(decision['management']['targets'])!=1:return None
    target=decision['management']['targets'][0]
    if target['kind']!='level':return None  # no invented future projection anchors
    seq=ext['setup_sequence'];rule=None
    if ext['setup_profile'] in PROFILES:
        if not seq:return None
        rule=dict(events_since=min(packet['evidence'][ref]['time'] for ref in ([seq['manipulation']['bar_id']] if seq['manipulation'] else [seq['objective_touch_bar_id']])),
            manipulation_level=seq['manipulation']['level'] if seq['manipulation'] else None,
            manipulation_side=seq['manipulation']['side'] if seq['manipulation'] else None,
            structure_swing_id=seq['structure']['swing_id'] if seq['structure'] else None,
            structure_role=seq['structure']['role'] if seq['structure'] else None,
            structure_break_mode=seq['structure']['mode'] if seq['structure'] else None,
            retracement_zone_id=seq['retracement_zone_id'],htf_objective=seq['objective'])
    smt=context.get('smt_selection');smt_rule=None
    if context['requires_smt']:
        if not smt:return None
        w=packet['evidence'][smt['current_mnq']]
        smt_rule=dict(reference_es=smt['reference_es'],reference_mnq=smt['reference_mnq'],current_window_minutes=(w['end']-w['start'])//60)
    watch=dict(armed=True,setup_profile=ext['setup_profile'],timeframes=[str(decision['timeframe_minutes'])],
        valid_for_seconds=ext['valid_for_seconds'],sequence_rule=rule,sequence_selector='selected_anchors',entry_mode=decision['entry_mode'],
        entry_limit_rule='selected_anchor',entry_anchor=ext['entry_anchor'],stop_rule='selected_anchor',stop_anchor=decision['initial_stop']['anchor'],
        stop_buffer_ticks=decision['initial_stop']['buffer_ticks'],thesis_invalidation=decision['invalidation'],
        final_target=target['anchor'],local_objective=ext['local_objective'],trade_kind=ext['trade_kind'],requires_smt=context['requires_smt'],smt_rule=smt_rule,
        directional_swing_id=decision['selected_swing']['evidence_id'] if decision['selected_swing'] else None,
        directional_swing_mode=decision['selected_swing']['break_mode'] if decision['selected_swing'] else 'close',
        poi_evidence_ids=decision['poi_evidence_ids'],reason='Reuse this explicitly reviewed context and fixed price priorities; '+decision['rationale'])
    validate_watch(packet,decision,watch)
    return watch


def gate(row,markets,*,cutoff,now):
    from entry_review import validate_decision
    from review_calendar import review_day
    out=dict(version=VERSION,state='BLOCKED',reasons=[],ai_run_id=row['id'],packet_id=row['packet_id'],source_cutoff=row['cutoff'],
        available_at=row.get('finished'),market_cutoff=cutoff,checked_at=now,expires_at=None,direction=None,orders_enabled=False)
    def blocked(reason):out['reasons']=[reason];return out,None,None,None
    if row['status']!='validated':return blocked('AI_CONTEXT_LATEST_REVIEW_NOT_VALIDATED')
    if not review_day(now)['new_reviews_allowed']:return blocked('AI_CONTEXT_NON_TRADING_REVIEW_DAY')
    try:
        source,started,finished=[row[k] for k in ('cutoff','started','finished')]
        if not all(type(x) in (int,float) and math.isfinite(x) for x in (source,started,finished,cutoff,now)) or not source<=started<=finished<=now or not source<=cutoff<=now:return blocked('AI_CONTEXT_INVALID_TIMESTAMPS')
        snapshot=json.loads(row['snapshot']);p=snapshot['packet'];d=json.loads(row['decision'])
        check_packet(p);validate_decision(p,d)
        if p['packet_id']!=row['packet_id'] or p['as_of']!=source:return blocked('AI_CONTEXT_EVIDENCE_MISMATCH')
        if not source<=snapshot['frozen_at']<=snapshot['processed_at']<=started:return blocked('AI_CONTEXT_INVALID_TIMESTAMPS')
        w=d.get('entry_watch') or derive_watch(p,d)
        if w is None:return out,p,d,None
        if not validate_watch(p,d,w):return blocked('AI_CONTEXT_WATCH_NOT_ARMED')
        out.update(expires_at=source+w['valid_for_seconds'],direction=d['context']['bias'],explanation=w['reason'])
        if datetime.fromtimestamp(source,NY).date()!=datetime.fromtimestamp(now,NY).date():return blocked('AI_CONTEXT_PREVIOUS_SESSION')
        if now>=out['expires_at'] or cutoff>=out['expires_at']:return blocked('AI_CONTEXT_EXPIRED')
        analysis=d['analysis_symbol']
        boundary=point_price(p,w['thesis_invalidation'],analysis)
        out.update(invalidation_price=boundary,invalidation_symbol=analysis)
        own=[b for b in markets[analysis] if source<=b['time'] and b['time']+60<=cutoff and b.get('received_at',b['time']+60)<=now]
        own.sort(key=lambda b:b['time'])
        if [b['time'] for b in own]!=list(range(source,cutoff,60)):return blocked('AI_CONTEXT_MONITORING_GAP')
        sign=1 if out['direction']=='long' else -1;adverse='low' if sign==1 else 'high';favorable='high' if sign==1 else 'low'
        target=point_price(p,w['final_target'],'MNQ')
        for b in own:
            if sign*(b[adverse]-boundary)<=0:
                out['invalidated_at']=b['time']+60;return blocked('AI_CONTEXT_PRICE_INVALIDATED')
        execution=[b for b in markets['MNQ'] if source<=b['time'] and b['time']+60<=cutoff and b.get('received_at',b['time']+60)<=now]
        execution.sort(key=lambda b:b['time'])
        if [b['time'] for b in execution]!=list(range(source,cutoff,60)):return blocked('MNQ_EXECUTION_MONITORING_GAP')
        for b in execution:
            if sign*(b[favorable]-target)>=0:
                out['invalidated_at']=b['time']+60;return blocked('AI_CONTEXT_OBJECTIVE_ALREADY_REACHED')
        out.update(state='PERMITTED',reasons=[])
        return out,p,d,w
    except (ValueError,KeyError,TypeError,IndexError,OverflowError):return blocked('AI_CONTEXT_AUDIT_REJECTED')


def sequence(packet,watch,trigger,direction):
    ev=packet['evidence'];rule=watch['sequence_rule'];family=watch['setup_profile']
    if family not in PROFILES:return None
    bars=sorted([(ref,b) for ref,b in ev.items() if b.get('kind')=='bar' and b.get('symbol')==trigger['symbol'] and b.get('timeframe')==1 and rule['events_since']<=b['time'] and b['available_at']<=trigger['time']],key=lambda x:x[1]['time'])
    def first(predicate):return next(((ref,b) for ref,b in bars if predicate(b)),None)
    if family==PROFILES[0]:
        level=point_price(packet,rule['manipulation_level'],trigger['symbol']);side=rule['manipulation_side'];known=ev[rule['manipulation_level']['evidence_id']]['available_at']
        m=first(lambda b:b['time']>=known and (b['high']>level if side=='above' else b['low']<level))
        if m is None:raise ValueError('WAIT_FOR_MANIPULATION_LEVEL_BREAK')
        swing=ev[rule['structure_swing_id']];field=swing['side'] if rule['structure_break_mode']=='wick' else 'close'
        s=first(lambda b:b['time']>=max(m[1]['time'],swing['available_at']) and (b[field]>swing['price'] if swing['side']=='high' else b[field]<swing['price']))
        if s is None:raise ValueError('WAIT_FOR_SELECTED_STRUCTURE_BREAK')
        zone_id=rule['retracement_zone_id'];after=s[1]['available_at']
        result=dict(manipulation=dict(level=rule['manipulation_level'],bar_id=m[0],side=side),
            structure=dict(swing_id=rule['structure_swing_id'],bar_id=s[0],role=rule['structure_role'],mode=rule['structure_break_mode']),objective=None,objective_touch_bar_id=None)
    else:
        objective=point_price(packet,rule['htf_objective'],trigger['symbol']);known=ev[rule['htf_objective']['evidence_id']]['available_at']
        touch=first(lambda b:b['time']>=known and b['low']<=objective<=b['high'])
        if touch is None:raise ValueError('WAIT_FOR_HTF_OBJECTIVE_TOUCH')
        zone_id=rule['retracement_zone_id']
        if zone_id is None:
            gaps=[]
            for ref,g in ev.items():
                if g.get('kind')!='FVG' or g.get('symbol')!=trigger['symbol'] or g.get('timeframe')!=15 or g.get('direction')!=('bull' if direction=='long' else 'bear'):continue
                formation=sorted([ev[x] for x in g['evidence_ids']],key=lambda b:b['time'])
                if len(formation)==3 and touch[1]['available_at']<=formation[1]['time'] and g['available_at']<=trigger['time']:gaps.append((ref,g))
            if not gaps:raise ValueError('WAIT_FOR_DIRECTIONAL_M15_DISPLACEMENT_GAP')
            zone_id=min(gaps,key=lambda x:(x[1]['available_at'],x[0]))[0]
        after=touch[1]['available_at']
        result=dict(manipulation=None,structure=None,objective=rule['htf_objective'],objective_touch_bar_id=touch[0])
    zone=ev[zone_id];lower,upper=(zone['lower'],zone['upper']) if 'lower' in zone else (zone['low'],zone['high'])
    retraced=[(ref,b) for ref,b in bars if b['time']>=max(after,zone['available_at']) and b['low']<=upper and b['high']>=lower]
    if not retraced:raise ValueError('WAIT_FOR_SELECTED_ZONE_RETRACEMENT')
    result.update(retracement_zone_id=zone_id,retracement_bar_id=retraced[-1][0])
    return result


def evaluate(row,markets,*,cutoff,now,budget,cap,daily_atr,analysis_symbol='MNQ'):
    """Return None for older direction-only reviews; otherwise audit each event."""
    from entry_review import compile_review,ASSESSMENTS
    from daily_atr import attach
    if row is None:return None
    if row['status']!='validated':return None
    raw=json.loads(row['decision'])
    if not raw.get('entry_watch') and not raw.get('entry_setup'):return None
    g,old,decision,watch=gate(row,markets,cutoff=cutoff,now=now)
    if raw.get('analysis_symbol')!=analysis_symbol:g.update(state='BLOCKED',reasons=['AI_ANALYSIS_SOURCE_MISMATCH'])
    symbols=('ES','NQ','MNQ') if analysis_symbol=='NQ' else ('ES','MNQ')
    data={s:[Bar(**{k:b[k] for k in ('time','open','high','low','close')}) for b in markets[s] if b['time']+60<=cutoff and b.get('received_at',b['time']+60)<=now] for s in symbols}
    p=build_v3_packet(data,cutoff)
    attach(p,daily_atr);current_id=p['packet_id']
    if g['state']!='PERMITTED':
        reasons=g['reasons'] or ['AI_CONTEXT_WATCH_NOT_ARMED']
        candidates=[dict(candidate_id=ref,cutoff=cutoff,packet_id=current_id,direction=e['direction'],timeframe=e['timeframe'],
            lower=e['lower'],upper=e['upper'],state='AI_CONTEXT_BLOCKED',reasons=reasons,context_ai_run_id=row['id'])
            for ref,e in p['evidence'].items() if e.get('kind')=='IFVG' and e.get('symbol')==analysis_symbol and e['available_at']==cutoff]
        return dict(state='AI_CONTEXT_BLOCKED',reasons=reasons,ai_context=g,orders_enabled=False,candidate_evaluations=candidates,cutoff=cutoff)
    # Carry original chosen anchors forward without changing their prices or
    # availability. A conflicting immutable evidence ID invalidates the audit.
    for ref,e in old['evidence'].items():
        if ref in p['evidence'] and p['evidence'][ref]!=e:raise ValueError('SAVED_CONTEXT_EVIDENCE_CHANGED')
        p['evidence'].setdefault(ref,deepcopy(e))
    scanner=None
    if watch.get('sequence_selector','selected_anchors')=='two_family_v1':
        from setup_scanner import SequenceScanner
        scanner=SequenceScanner(markets,cutoff,now)
        for event in scanner.events:
            for ref,e in scanner.evidence_for(event,{}).items():
                if ref in p['evidence'] and p['evidence'][ref]!=e:raise ValueError('SCANNER_EVIDENCE_CONFLICT')
                p['evidence'].setdefault(ref,e)
    events=[(ref,e) for ref,e in p['evidence'].items() if e.get('kind')=='IFVG' and e.get('symbol')==analysis_symbol and e['available_at']==cutoff]
    if not events and not raw.get('entry_watch'):return None  # permit the original reviewed entry while it remains valid
    provenance=dict(ai_run_id=row['id'],packet_id=old['packet_id'],cutoff=old['as_of'],available_at=math.ceil(row['finished']),
        expires_at=g['expires_at'],rule_fingerprint=fingerprint(watch),origin='saved_context_rule')
    p['context_provenance']=provenance;p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
    evaluations=[];ready=[]
    for ref,event in sorted(events,key=lambda x:(x[1]['available_at'],x[0])):
        audit=dict(candidate_id=ref,cutoff=cutoff,packet_id=current_id,direction=event['direction'],timeframe=event['timeframe'],
            lower=event['lower'],upper=event['upper'],state='BLOCKED',reasons=[],context_ai_run_id=row['id'],setup_profile=watch['setup_profile'])
        evaluations.append(audit)
        try:
            if event['direction']!=g['direction']:raise ValueError('AI_CONTEXT_DIRECTION_CONFLICT')
            if str(event['timeframe']) not in watch['timeframes']:raise ValueError('SAVED_CONTEXT_TIMEFRAME_NOT_SELECTED')
            refs=event['evidence_ids'];trigger=p['evidence'][refs[-1]]
            if provenance['available_at']>trigger['time']:raise ValueError('CONTEXT_NOT_AVAILABLE_BEFORE_CONFIRMATION_BAR')
            setup_id=None
            if scanner is not None:
                measured,evidence=scanner.find(watch['setup_profile'],event)
                seq=measured[4];setup_id=measured[3]
                for key,e in evidence.items():
                    if key in p['evidence'] and p['evidence'][key]!=e:raise ValueError('SCANNER_EVIDENCE_CONFLICT')
                    p['evidence'].setdefault(key,e)
                audit.update(setup_id=setup_id,sequence=seq,measurements=measured[5],scanner_version='two-family-sequence-scanner-v1')
            else:seq=sequence(p,watch,trigger,event['direction'])
            sign=1 if event['direction']=='long' else -1;adverse='low' if sign==1 else 'high'
            def execution_ref(ref):
                b=p['evidence'][ref]
                matches=[key for key,e in p['evidence'].items() if e.get('kind')=='bar' and e.get('symbol')=='MNQ' and e.get('time')==b.get('time') and e.get('timeframe')==b.get('timeframe') and e['available_at']==b['available_at']]
                if len(matches)!=1:raise ValueError('PAIRED_MNQ_EXECUTION_ANCHOR_REQUIRED')
                return matches[0]
            if watch['stop_rule']=='selected_anchor':stop_anchor=watch['stop_anchor']
            elif watch['stop_rule']=='confirmation_extreme':stop_anchor=dict(evidence_id=execution_ref(refs[-1]),field=adverse)
            elif watch['stop_rule']=='retracement_extreme':
                if seq is None:raise ValueError('RETRACEMENT_STOP_REQUIRES_SELECTED_SEQUENCE')
                stop_anchor=dict(evidence_id=execution_ref(seq['retracement_bar_id']),field=adverse)
            else:
                choices=refs+([seq['retracement_bar_id']] if seq else [])
                if seq and seq['manipulation']:choices += [seq['manipulation']['bar_id'],seq['structure']['bar_id']]
                choices=[execution_ref(r) for r in choices]
                anchor=min(choices,key=lambda r:(sign*p['evidence'][r][adverse],r));stop_anchor=dict(evidence_id=anchor,field=adverse)
            stop=point_price(p,stop_anchor,'MNQ')-sign*.25*watch['stop_buffer_ticks']
            anchor=watch['entry_anchor']
            if watch['entry_limit_rule']=='confirmation_open':anchor=dict(evidence_id=execution_ref(refs[-1]),field='open')
            elif watch['entry_limit_rule']=='inverted_gap_edge':anchor=dict(evidence_id=ref,field='upper' if sign==1 else 'lower')
            claims=deepcopy(decision['context']['claims'])
            claims['confirmation_accepted']=dict(value=True,evidence_ids=[ref,refs[-1]],reason='Measured strict inversion close under the saved sequence policy')
            ctx=dict(decision['context'],packet_id=p['packet_id'],as_of=cutoff,bias=event['direction'],decision='candidate',setup='inversion',
                selected_trigger_id=ref,claims=claims,requires_smt=watch['requires_smt'],missing_context=[])
            if watch['requires_smt']:
                from context_geometry import measure_smt
                smt=watch['smt_rule'];start=cutoff-60*smt['current_window_minutes'];current={}
                for sym in ('ES',analysis_symbol):
                    bars=[b for b in markets[sym] if start<=b['time']<cutoff and b.get('received_at',b['time']+60)<=now]
                    if [b['time'] for b in bars]!=list(range(start,cutoff,60)):raise ValueError('CURRENT_SMT_PAIRED_DATA_REQUIRED')
                    key=f'{sym}:WATCH_WINDOW:{start}:{cutoff}';hi=max(b['high'] for b in bars);lo=min(b['low'] for b in bars)
                    p['evidence'][key]=dict(kind='window',symbol=sym,start=start,end=cutoff,available_at=cutoff,complete=True,
                        high=hi,low=lo,high_times=[b['time'] for b in bars if b['high']==hi],low_times=[b['time'] for b in bars if b['low']==lo])
                    current[sym]=key
                ctx['smt_selection']=dict(reference_es=smt['reference_es'],reference_mnq=smt['reference_mnq'],current_es=current['ES'],current_mnq=current[analysis_symbol],side='low' if sign==1 else 'high')
                if measure_smt(p,ctx['smt_selection'],event['direction'])['intact'] is not True:raise ValueError('CURRENT_SMT_NOT_CONFIRMED')
            else:ctx['smt_selection']=None
            p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});ctx['packet_id']=p['packet_id']
            target=dict(kind='level',anchor=watch['final_target'],extreme=None,price=point_price(p,watch['final_target'],'MNQ'),quantity=None,reason='Saved review selected final objective')
            adapted=dict(decision,context=ctx,initial_stop=dict(anchor=stop_anchor,buffer_ticks=watch['stop_buffer_ticks'],price=stop,reason='Saved explicit '+watch['stop_rule']+' rule'),
                invalidation=watch['thesis_invalidation'],timeframe_minutes=event['timeframe'],poi_evidence_ids=watch['poi_evidence_ids'],entry_mode=watch['entry_mode'],
                require_swing_break=watch['directional_swing_id'] is not None,
                selected_swing=dict(evidence_id=watch['directional_swing_id'],break_mode=watch['directional_swing_mode'],reason='Saved directional swing requirement') if watch['directional_swing_id'] else None,
                management=dict(policy='structural_trail',targets=[target],partial_policy='one_third_original_floor',time_exit_at=None,reason='Saved user management policy'))
            adapted['entry_setup']=dict(setup_profile=watch['setup_profile'],setup_sequence=seq,entry_anchor=anchor,
                entry_mode_reason='Saved context selected '+watch['entry_mode'],valid_for_seconds=60,trade_kind=watch['trade_kind'],
                assessments={k:dict(accepted=True,reason='Saved context plus newly measured sequence/inversion; '+watch['reason'],evidence_ids=list(dict.fromkeys(watch['poi_evidence_ids']+[ref,refs[-1]]))) for k in ASSESSMENTS},
                local_objective=watch['local_objective'])
            compiled=compile_review(p,adapted,selected_at=now,now=now,budget=budget,cap=cap,
                assessment_origin='saved_context_rule',context_provenance=provenance)
            # Fresh inversion entry lifetime never extends the context lifetime.
            s=compiled['selected']['selection'];s['context_valid_until']=g['expires_at'];s['entry_valid_until']=s['context_valid_until']
            if setup_id is not None:s['setup_id']=setup_id
            from entry_rules import compile_entry
            compiled['compiled']=compile_entry(compiled['packet'],s,compiled['selected']['observations'],now=now)
            compiled['state']='PLAN_READY' if compiled['compiled']['plan'] else 'COMPILER_BLOCKED';compiled['reasons']=compiled['compiled']['missing']+compiled['compiled']['errors']
            audit.update(state=compiled['state'],reasons=compiled['reasons'],explanation='Saved review '+row['id']+'; fresh '+str(event['timeframe'])+'m inversion; '+watch['setup_profile']+'; '+('shared May sequence selector' if scanner is not None else 'explicitly selected anchors'))
            if compiled['compiled']['plan']:
                audit.update(entry=compiled['compiled']['plan']['entry_reference']['price'],stop=compiled['compiled']['price_review']['stop'],target=compiled['compiled']['price_review']['target'],entry_mode=watch['entry_mode'])
                ready.append((event,compiled))
        except (ValueError,KeyError,TypeError,IndexError) as exc:audit['reasons']=[str(exc)]
    if ready:
        result=max(ready,key=lambda x:(p['evidence'][x[0]['evidence_ids'][2]]['available_at'],x[0]['zone_id']))[1]
        chosen=result['selected']['selection']['trigger_event_id']
        for candidate in evaluations:
            if candidate['state']=='PLAN_READY' and candidate['candidate_id']!=chosen:
                candidate.update(state='QUALIFIED_NOT_SELECTED',reasons=['OTHER_QUALIFIED_INVERSION_FORMED_LATER'])
    else:result=dict(state='WATCHING_CONTEXT' if not evaluations else 'CONTEXT_CANDIDATES_BLOCKED',reasons=['WAIT_FOR_NEW_SELECTED_INVERSION'] if not evaluations else sorted({r for e in evaluations for r in e['reasons']}),orders_enabled=False,cutoff=cutoff)
    result.update(candidate_evaluations=evaluations,ai_context=g,context_watch=dict(version=VERSION,policy=watch,provenance=provenance,
        selection_rule='latest-formed qualified inversion first',sequence_selector=watch.get('sequence_selector','selected_anchors'),discretion_reassessed=False),daily_atr=daily_atr)
    return result
