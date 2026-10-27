"""Exact recovery AI contract. A selection is advice, never an order permit.

Uses the existing market packet and shared review journal. This module contains
no transport and cannot create orders. Caller must still run the existing final
quote/resting-policy, account, Guard, broker and native-protection gates.
"""
import copy
import json
import math
import directional_ict_context
import directional_recovery_plans as plans

PROMPT_VERSION = 'directional-recovery-ict-wick-v3'
PROMPT = '''Review one existing Directional opportunity after its original LIMIT
expired unfilled. Select at most ONE supplied eligible plan or return WAIT/REJECT.
Do not create a setup, choose another candle, change LIMIT/SL/TP, quantity or expiry.
The source candle is the FIRST chronological candle of each selected three-candle
FVG, explicitly bound by FIRST_FVG_CANDLE_WICK_V1. LONG stop is below its low wick,
SHORT stop above its high wick, using the supplied server buffer.
For a NEW_BOS_FVG_RETEST explicitly bound to CONFIRMED_SCANNER_SWING_V1, use
the supplied confirmed swing candle of that new impulse instead. Both anchors
are deterministic candidates: ALLOW only if the cited extreme actually represents
structural invalidation, otherwise WAIT/REJECT. Never invent a substitute anchor.
Do not substitute the gap edge, third confirmation candle, old stop, or a fixed
one-point stop. If the supplied source candle is not a valid ICT invalidation
anchor, return WAIT or REJECT and explain why. FIBO_50 uses the ORIGINAL anchors
with NO offset; FVG entry is the exact midpoint. Target is exactly 2R gross.
Review all ICT topics: HTF thesis and draw, premium/discount, liquidity sweeps,
MSS/BOS/displacement, FVG/IFVG and mitigation, ATR, NY session, ES/NQ SMT,
opposing liquidity and target room, entry/invalidation geometry. Trend alone
does not disqualify reversal. ATR efficiency alone never proves ICT confirmation.
Cite only supplied evidence IDs and explicitly mark missing evidence UNKNOWN.
For ALLOW, fvg_ifvg, opposing_liquidity and invalidation_geometry must SUPPORT
the selected plan with cited observations; decisive uncertainty requires WAIT.
Confidence is subjective model confidence, NOT a win probability. Historical
OHLC touches do not establish executable quotes, fills or cancellation. You do
not certify account risk or broker protection. No future candles are available.
Copy the exact request ID, opportunity ID, plan version and selected plan binding.
WAIT, REJECT or CANCEL uses null selected_plan. CANCEL means the original
opportunity is no longer valid. Return only the requested JSON schema.'''


def obj(fields):
    return dict(type='object', properties=fields, required=list(fields), additionalProperties=False)


def schema():
    text = dict(type='string'); number = dict(type='number')
    assessment = obj(dict(assessment=dict(type='string', enum=['SUPPORTS','CONTRADICTS','MIXED','UNKNOWN']),
        evidence_ids=dict(type='array', items=text), reason_pl=text,
        missing_evidence=dict(type='array', items=text)))
    binding = obj(dict(plan_id=text, **{k:dict(type='integer') if k.endswith('_ms') else
        number if k.endswith('_price') else text for k in plans.GEOMETRY}))
    return obj(dict(request_id=text, opportunity_id=text, plan_version=dict(type='integer', enum=[2]),
        decision=dict(type='string', enum=['ALLOW','WAIT','REJECT','CANCEL']),
        selected_plan=dict(anyOf=[binding, dict(type='null')]), reason_pl=text,
        confidence=dict(anyOf=[dict(type='number',minimum=0,maximum=1),dict(type='null')]),
        ict_assessment=obj({topic:copy.deepcopy(assessment) for topic in directional_ict_context.TOPICS})))


def binding(plan):
    return dict(plan_id=plan['plan_id'], **{k:plan[k] for k in plans.GEOMETRY})


def request(original, packet, candidates, model):
    import directional_hybrid_context
    if not model:
        raise ValueError('REVIEW_MODEL_UNKNOWN')
    # Validate ALL market evidence, including ES/HTF observations not used by
    # price geometry. Future context must never leak into an otherwise valid plan.
    derived = directional_hybrid_context.build(packet)
    eligible = []
    for candidate in candidates:
        if candidate['status'] == 'ELIGIBLE_FOR_AI':
            plan = candidate['plan']; plans.verify_plan(plan)
            if plan['original_fingerprint'] != plans.digest(plans.original_view(original)):
                raise ValueError('RECOVERY_ORIGINAL_CHANGED')
            eligible.append(plan)
    if not eligible:
        raise ValueError('NO_ELIGIBLE_RECOVERY_PLAN')
    if len(eligible)>3 or len({p['kind'] for p in eligible}) != len(eligible):
        raise ValueError('DUPLICATE_RECOVERY_PLAN')
    # Copy market-only fields; no raw account/credentials/ledger/future outcomes.
    market = {k:copy.deepcopy(packet[k]) for k in ('as_of','evidence','ict_context','hybrid_context') if k in packet}
    market['hybrid_context'] = derived
    context = dict(prompt_version=PROMPT_VERSION, opportunity_id=original['order_id'],
                   review_expires_at=min(min(p['activation_ms'] for p in eligible)/1000+90,
                                         original['activation_ms']/1000+1200),
                   plan_version=2, original=plans.original_view(original), market=market,
                   plans=eligible, unavailable_plans=[dict(kind=c['kind'], reason=c['reason'])
                       for c in candidates if c['status'] != 'ELIGIBLE_FOR_AI'])
    context['request_id'] = plans.digest(context)
    return dict(model=model,stream=True,store=False,input=[dict(role='system',content=PROMPT),
        dict(role='user',content=json.dumps(context,sort_keys=True,allow_nan=False))],
        text=dict(format=dict(type='json_schema',name='directional_recovery',strict=True,schema=schema())))


def unpack(payload):
    ctx=json.loads(payload['input'][1]['content'])
    if ctx['request_id'] != plans.digest({k:v for k,v in ctx.items() if k!='request_id'}):
        raise ValueError('RECOVERY_REQUEST_CHANGED')
    return ctx


def validate(decision, payload):
    # Reuse the same strict JSON-schema validator as the original AI worker.
    from ab_ai_context import schema_check
    schema_check(decision, schema())
    ctx=unpack(payload)
    if any(decision[k]!=ctx[k] for k in ('request_id','opportunity_id','plan_version')):
        raise ValueError('RECOVERY_REVIEW_BINDING_MISMATCH')
    if not decision['reason_pl'].strip():
        raise ValueError('RECOVERY_REASON_REQUIRED')
    if decision['confidence'] is not None and (type(decision['confidence']) not in (int,float)
            or not math.isfinite(decision['confidence']) or not 0<=decision['confidence']<=1):
        raise ValueError('RECOVERY_CONFIDENCE_INVALID')
    for topic, item in decision['ict_assessment'].items():
        if not item['reason_pl'].strip() or any(ref not in ctx['market']['evidence'] for ref in item['evidence_ids']):
            raise ValueError('RECOVERY_ICT_EVIDENCE_INVALID')
        if item['assessment']=='UNKNOWN':
            if not item['missing_evidence']:raise ValueError('RECOVERY_UNKNOWN_NOT_EXPLAINED')
        elif not item['evidence_ids']:
            raise ValueError('RECOVERY_ICT_CLAIM_WITHOUT_EVIDENCE')
        if decision['decision']=='ALLOW' and topic in ('fvg_ifvg','opposing_liquidity','invalidation_geometry'):
            if item['assessment']!='SUPPORTS':raise ValueError('RECOVERY_DECISIVE_CONTEXT_NOT_SUPPORTED')
    if decision['decision']!='ALLOW':
        if decision['selected_plan'] is not None:raise ValueError('NON_ALLOW_SELECTED_A_PLAN')
        return None
    selected=next((p for p in ctx['plans'] if binding(p)==decision['selected_plan']), None)
    if selected is None:raise ValueError('AI_CHANGED_RECOVERY_GEOMETRY')
    plans.verify_plan(selected)
    return selected


def revalidate(plan, payload, fresh_packet, now_ms):
    """A saved ALLOW cannot resurrect expiry, revised candles or opposition."""
    import directional_hybrid_context
    from directional_hybrid_lifecycle import structure_event_reason
    ctx=unpack(payload); plans.verify_plan(plan)
    if binding(plan) not in [binding(p) for p in ctx['plans']]:
        raise ValueError('RECOVERY_PLAN_NOT_REVIEWED')
    if not plan['activation_ms']<=now_ms<plan['expiry_ms']:
        raise ValueError('RECOVERY_EXPIRED')
    old=ctx['market']
    if fresh_packet['as_of']<old['as_of']:raise ValueError('RECOVERY_CONTEXT_REGRESSED')
    for key,e in old['evidence'].items():
        if key not in fresh_packet['evidence'] or fresh_packet['evidence'][key]!=e:
            raise ValueError('RECOVERY_CONTEXT_REVISED_OR_MISSING')
    # Use the same direction-aware rule as the original setup review.
    fresh=directional_hybrid_context.build(fresh_packet)
    blocked=structure_event_reason(
        [e for e in fresh['structure_events']['value'] if e['available_at']>old['as_of']],
        plan['direction'])
    if blocked:
        raise ValueError(blocked.replace('HYBRID_', 'RECOVERY_', 1))
    rebuilt=plans.build(ctx['original'],fresh_packet,plan['selection'],now_ms,
                        stop_buffer_points=plan['stop_anchor']['buffer_points'])
    if rebuilt['status']!='ELIGIBLE_FOR_AI':raise ValueError(rebuilt['reason'])
    current=rebuilt['plan']
    # Activation changes with time; source, geometry and original hard deadline cannot.
    for key in ('direction','contract','entry_price','stop_price','target_price','expiry_ms','stop_anchor','evidence'):
        if current[key]!=plan[key]:raise ValueError('RECOVERY_PLAN_CHANGED_AFTER_AI')
    return plan
