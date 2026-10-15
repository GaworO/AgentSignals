"""Structured entry choices on the existing AI call; legacy v3 audits still read.

No model call, price discovery or broker transport here. Quantity comes from
the current account guard, never the model. Model judgments retain provenance.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
from context_v3 import (output_schema as legacy_schema, validate_decision as legacy_validate,
    check_schema, object_schema, nullable, enum, array, STR, point_price)
from entry_rules import compile_entry, fingerprint
from entry_patterns import PROFILES
from source_rules import VERSION
from risk_sizing import contracts_for_stop

REVISION = 'tanja-entry-sequences-20261013-v1'
ASSESSMENTS = ('setup_context','confirmation_strength','entry_timing','stop_invalidation','target_priority')
PROMPT = '''
User-approved entry-sequence extension, 13 October 2026:
Both manipulation_retracement_inversion and htf_fvg_retracement_inversion can
be LONG or SHORT. Direction does not select the family. M1 strict inversion
close is the confirmation for these families; no additional M5 close is required.
For manipulation: select a preexisting liquidity level and its break, a known
swing and its break, then a retracement into a selected previously known zone
(gap, candle range or complete range window), then M1
inversion. Structure role is explicit: liquidity_sweep (low for a long, high for
a short) or directional_break (high for a long, low for a short). A low break
alone is not bearish confirmation; a bullish inversion can confirm its reversal.
For HTF: select a preexisting H1/H4 objective, an M1 touch of that objective,
then a directional M15 displacement forming a gap, an M1 retracement into that
gap, then M1 inversion. Objective touch must precede the middle M15 candle.
The pre-confirmation retracement must close before the inversion candle starts.
All profiles can enter immediately after confirmation OR wait for a further
retracement. For retracement select an exact MNQ evidence point at a better
price than the confirmation close; no invented midpoint, fill or price. Pending
entry cancels on thesis invalidation, final objective, expiry or news blackout.
Provide entry_setup with explicit assessments and evidence, or null if unresolved.
Use setup_sequence=null for older selected_inversion/m5_momentum profiles.
Unused sequence fields must be null. valid_for_seconds is an explicit research
lifetime, 60–900 seconds from trigger close, not a transcript-derived constant.
trade_kind is continuation or reversal. ATR exhaustion blocks new continuation
entries only; it does not force an exit. ATR is checked by the software, never
invent a measurement. Unknown ATR blocks continuation plans.
Quantity/risk come from the reported account. Leave initial_quantity and target
quantities null. Select one final target in management.targets; use
entry_setup.local_objective for an optional earlier ES/MNQ partial objective.
User policy: take floor(original filled contracts/3) at that local objective;
skip if zero. Structural trail requires a separate confirmed swing selection.
The new families do not require an unrelated liquidity sweep/range-edge claim:
leave those legacy claims unknown when inapplicable. Do not manufacture true
claims. Higher-timeframe context, confirmation and intact structure are required.
This extension implements the user's generalization; it is not proof of Tanja's
discretionary choices or evidence of profitability.
'''


def output_schema():
    schema = legacy_schema()
    point = object_schema(dict(evidence_id=STR,field=enum(['high','low','open','close','price','lower','upper'])))
    assessment = object_schema(dict(accepted={'type':'boolean'},reason=STR,evidence_ids=array(STR)))
    sequence = object_schema(dict(retracement_zone_id=STR,retracement_bar_id=STR,
        manipulation=nullable(object_schema(dict(level=point,bar_id=STR,side=enum(['above','below'])))),
        structure=nullable(object_schema(dict(swing_id=STR,bar_id=STR,role=enum(['liquidity_sweep','directional_break']),mode=enum(['wick','close'])))),
        objective=nullable(point),objective_touch_bar_id={'type':['string','null']}))
    setup = object_schema(dict(setup_profile=enum(list(PROFILES)+['selected_inversion','m5_momentum']),
        setup_sequence=nullable(sequence),entry_anchor=nullable(point),entry_mode_reason=STR,
        valid_for_seconds={'type':'integer'},trade_kind=enum(['continuation','reversal']),
        assessments=object_schema({k:assessment for k in ASSESSMENTS}),
        local_objective=nullable(object_schema(dict(anchor=point,symbol=enum(['ES','MNQ']),reason=STR)))))
    schema['properties']['entry_setup']=nullable(setup)
    schema['required'].append('entry_setup')
    from context_watch import schema as watch_schema
    schema['properties']['entry_watch']=watch_schema()
    schema['required'].append('entry_watch')
    return schema


def compile_review(packet, decision, *, selected_at, now, budget, cap, available_at=None,
                   assessment_origin='model_review',context_provenance=None):
    """Transform selected evidence, with account sizing, into a compiler input."""
    ext=decision['entry_setup']; context=decision['context']; direction=context['bias']
    event=packet['evidence'][context['selected_trigger_id']]
    refs=event.get('evidence_ids', [])
    if len(refs)!=4 or event['available_at']!=packet['as_of']:
        raise ValueError('LATEST_TRIGGER_WITH_FORMATION_BARS_REQUIRED')
    lifetime=ext['valid_for_seconds']
    if type(lifetime) is not int or not 60<=lifetime<=900:
        raise ValueError('ENTRY_LIFETIME_60_TO_900_SECONDS_REQUIRED')
    p=deepcopy(packet);p['clock_id']='tanja-entry:unix_utc'
    p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
    analysis=decision['analysis_symbol']
    trigger=packet['evidence'][refs[-1]]
    execution_refs=[ref for ref,b in packet['evidence'].items() if b.get('kind')=='bar' and b.get('symbol')=='MNQ'
        and b.get('time')==trigger['time'] and b.get('timeframe')==trigger['timeframe'] and b.get('available_at')==trigger['available_at']]
    if len(execution_refs)!=1:raise ValueError('MNQ_CONFIRMATION_REFERENCE_REQUIRED')
    entry = dict(evidence_id=execution_refs[0],field='close') if decision['entry_mode']=='immediate_after_confirmation' else ext['entry_anchor']
    if entry is None:raise ValueError('RETRACEMENT_ENTRY_ANCHOR_REQUIRED')
    stop=decision['initial_stop']
    if stop is None:raise ValueError('INITIAL_STOP_SELECTION_REQUIRED')
    targets=decision['management']['targets']
    if len(targets)!=1:raise ValueError('ONE_FINAL_TARGET_REQUIRED_USE_LOCAL_OBJECTIVE_FOR_PARTIAL')
    if decision['management']['policy']!='structural_trail':raise ValueError('SOURCE_STRUCTURAL_TRAIL_REQUIRED')
    q=contracts_for_stop(point_price(packet,entry,'MNQ'),stop['price'],direction,budget,cap)
    if q<1:raise ValueError('BUDGET_CANNOT_FUND_ONE_CONTRACT')
    partials=[]; local=ext['local_objective']
    if local:
        e=packet['evidence'][local['anchor']['evidence_id']]
        partials=[dict(id='local-objective',quantity_policy='one_third_original_floor',reason=local['reason'],
            objective=dict(symbol=local['symbol'],price=point_price(packet,local['anchor'],local['symbol']),
                available_at=e['available_at'],source=local['anchor']['evidence_id'],direction='above' if direction=='long' else 'below'))]
    s=dict(id=f'{REVISION}:{packet["packet_id"]}:{event["available_at"]}:{direction}',
        packet_id=p['packet_id'],clock_id=p['clock_id'],selected_at=selected_at,selection_available_at=selected_at if available_at is None else available_at,
        confirmation_policy=VERSION,selection_origin='explicit_context_review',analysis_symbol=analysis,execution_symbol='MNQ',
        direction=direction,timeframe_minutes=decision['timeframe_minutes'],setup_profile=ext['setup_profile'],
        setup_sequence=ext['setup_sequence'],gap_bar_ids=refs[:3],trigger_bar_id=refs[-1],
        trigger_event_id=context['selected_trigger_id'],
        requires_smt=context['requires_smt'],requires_swing_break=decision['require_swing_break'],requires_followthrough=False,
        entry_mode=decision['entry_mode'],entry_mode_reason=ext['entry_mode_reason'],entry_reference=entry,
        context_valid_until=event['available_at']+lifetime,entry_valid_until=event['available_at']+lifetime,
        cancel_condition='Cancel on selected invalidation, final target, expiry or scheduled-news blackout',
        initial_stop=dict(mode='selected_structural_anchor',anchors=[stop['anchor']],buffer_ticks=stop['buffer_ticks'],reason=stop['reason']),
        thesis_invalidation=decision['invalidation'],
        target=targets[0],initial_quantity=q,risk_budget_usd=budget,trade_kind=ext['trade_kind'],
        management_spec=dict(version=VERSION,partials=partials,trail='explicit_confirmed_swing',additions='separate_review_required'),
        assessments={k:dict(v,available_at=packet['as_of'],origin=assessment_origin) for k,v in ext['assessments'].items()})
    if context_provenance is not None:s['context_provenance']=context_provenance
    if decision['selected_swing']:
        s.update(swing_id=decision['selected_swing']['evidence_id'],swing_break_mode=decision['selected_swing']['break_mode'])
    prefix='bullish' if direction=='long' else 'bearish'
    fields={prefix+'_thesis_supported':context['claims']['higher_timeframe_context_valid'],
        'poi_reached':dict(value=ext['assessments']['setup_context']['accepted'],evidence_ids=decision['poi_evidence_ids']),
        'nasdaq_'+prefix+'_confirmation':context['claims']['confirmation_accepted'],
        'target_identified':dict(value=ext['assessments']['target_priority']['accepted'],evidence_ids=[targets[0]['anchor']['evidence_id']])}
    if ext['setup_profile'] not in PROFILES:
        fields['sellside_event_observed' if direction=='long' else 'buyside_event_observed']=context['claims']['liquidity_event_complete']
    if context['requires_smt']:
        from context_v3 import validate_decision as base_validate
        checked=base_validate(packet,{k:v for k,v in decision.items() if k not in ('entry_setup','entry_watch')})
        smt=checked['geometry'].get('smt',{})
        fields['selected_smt_present']=dict(value=smt.get('intact') is True,
            evidence_ids=[context['smt_selection'][k] for k in ('reference_es','reference_mnq','current_es','current_mnq')])
    obs=[dict(field=k,value=v['value'],evidence_ids=v['evidence_ids'],packet_id=p['packet_id'],clock_id=p['clock_id'],
              available_at=packet['as_of'],source=assessment_origin) for k,v in fields.items()]
    result=compile_entry(p,s,obs,now=now)
    return dict(packet=p,selected=dict(selection=s,observations=obs,audit=dict(recipe=ext['setup_profile'],
        timeframe=s['timeframe_minutes'],quantity=q,poi='explicit_context_review',selection_origin='explicit_context_review',
        stop=(result['price_review'] or {}).get('stop'),target=dict(price=(result['price_review'] or {}).get('target'),reason=targets[0]['reason']))),
        compiled=result,state='PLAN_READY' if result['plan'] else 'COMPILER_BLOCKED',
        reasons=result['missing']+result['errors'],cutoff=packet['as_of'],orders_enabled=False,semantic_fidelity_verified=False)


def validate_decision(packet, decision):
    base={k:v for k,v in decision.items() if k not in ('entry_setup','entry_watch')}
    result=legacy_validate(packet,base)
    if 'entry_setup' not in decision and 'entry_watch' not in decision:return result  # immutable historical audits
    selected_schema=output_schema()
    for name in ('entry_setup','entry_watch'):
        if name not in decision:
            selected_schema['properties'].pop(name);selected_schema['required'].remove(name)
    if decision.get('entry_watch') is not None and 'sequence_selector' not in decision['entry_watch']:
        old_watch=selected_schema['properties']['entry_watch']['anyOf'][0]
        old_watch['properties'].pop('sequence_selector');old_watch['required'].remove('sequence_selector')
    check_schema(decision,selected_schema)
    if decision.get('entry_watch') is not None:
        from context_watch import validate_watch
        result['context_watch_verified']=validate_watch(packet,decision,decision['entry_watch'])
    ext=decision.get('entry_setup')
    if ext is None or decision['context']['decision']!='candidate':return result
    context=decision['context']
    if ext['setup_profile'] in PROFILES:
        conditions=all(context['claims'][k]['value'] is True for k in
            ('higher_timeframe_context_valid','confirmation_accepted','structure_intact'))
        result['context_review_state']='CONDITIONS_MET' if conditions else 'WAIT'
        # Range edge/liquidity event are replaced by the measured sequence.
        if 'range_selection' not in decision['missing_fields']:
            result['missing_plan_fields']=[x for x in result['missing_plan_fields'] if x!='range_selection']
    # Quantity is filled by current account sizing. No other missing information
    # is waived, including explicit entries in decision.missing_fields.
    if 'initial_quantity_and_risk_budget' not in decision['missing_fields']:
        result['missing_plan_fields']=[x for x in result['missing_plan_fields'] if x!='initial_quantity_and_risk_budget']
    if result['missing_plan_fields'] or result['context_review_state']!='CONDITIONS_MET':return result
    compiled=compile_review(packet,decision,selected_at=packet['as_of'],now=packet['as_of'],budget=1_000_000,cap=1)
    if not compiled['compiled']['plan']:
        if compiled['reasons'] and all(x in ('DAILY_ATR_MEASUREMENT_REQUIRED','DAILY_ATR_REACHED_CONTINUATION_BLOCKED') for x in compiled['reasons']):
            result['missing_plan_fields']=sorted(set(result['missing_plan_fields']+compiled['reasons']))
            return result
        raise ValueError('ENTRY_SEQUENCE_OR_PRICE_PLAN_REJECTED: '+','.join(compiled['reasons']))
    result.update(entry_contract=REVISION,entry_setup_verified=True,order_readiness='REQUIRES_CURRENT_ACCOUNT_NEWS_AND_PRICE_CHECKS')
    return result


def from_saved_review(row, *, now, budget, cap):
    if row is None or row.get('status')!='validated':return None
    d=json.loads(row['decision'])
    if not d.get('entry_setup') or d['context']['decision']!='candidate':return None
    p=json.loads(row['snapshot'])['packet']
    checked=validate_decision(p,d)
    if not checked.get('entry_setup_verified'):return None
    available=math.ceil(row['finished'])
    if available>now:raise ValueError('ENTRY_REVIEW_NOT_AVAILABLE')
    return compile_review(p,d,selected_at=now,available_at=available,now=now,budget=budget,cap=cap)
