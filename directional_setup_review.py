"""Causal setup descriptions for observational AI reviews; no execution API."""
import json
import math
import copy
import directional_ict_context

PROMPT_VERSION = 'directional-ict-setup-v2'


def describe(order, contract):
    if order.get('strategy') != 'AB_DIRECTIONAL':
        raise ValueError('directional_setup_required')
    return describe_geometry(order, contract)


def describe_geometry(order, contract):
    """Shared geometry serialization; strategy-specific callers validate eligibility."""
    direction = order['direction']
    if direction not in ('LONG', 'SHORT'):
        raise ValueError('invalid_setup_direction')
    levels = {k: float(order[k]) for k in ('entry_price', 'stop_price', 'target_price')}
    if any(not math.isfinite(v) or v <= 0 for v in levels.values()):
        raise ValueError('invalid_setup_prices')
    sign = 1 if direction == 'LONG' else -1
    risk = sign * (levels['entry_price'] - levels['stop_price'])
    reward = sign * (levels['target_price'] - levels['entry_price'])
    if risk <= 0 or reward <= 0:
        raise ValueError('invalid_setup_geometry')
    activation, expiry = int(order['activation_ms']), int(order['expiry_ms'])
    if activation >= expiry:
        raise ValueError('invalid_setup_expiry')
    if not contract:
        raise ValueError('setup_contract_required')
    payload = json.loads(order.get('payload_json') or '{}')
    snapshot = payload.get('v3_snapshot') or {}
    # Whitelist causal fields. No account, fills, replay outcomes or whole ledger.
    result = dict(order_id=str(order['order_id']), candidate_id=str(order['candidate_id']),
                  strategy=order['strategy'], direction=direction, contract=contract,
                  activation_ms=activation, expiry_ms=expiry, **levels,
                  risk_points=risk, target_r=reward/risk,
                  alternative_2r=levels['entry_price']+sign*2*risk,
                  alternative_3r=levels['entry_price']+sign*3*risk,
                  source_level=snapshot.get('source_level'), fvg_edge=snapshot.get('fvg_edge'),
                  setup_category=payload.get('setup_category'), entry_rules=payload.get('entry_rules'),
                  frozen_context=directional_ict_context.frozen_context(order, payload))
    if len(json.dumps(result, allow_nan=False)) > 40000:
        raise ValueError('setup_too_large')
    return result


def prompt(base):
    return base.replace(
        'For each side ALLOW means no contextual objection to a later valid A/B Directional setup;',
        'For the direction of the supplied setup ALLOW means specific contextual support for this setup;'
    ).replace(
        'No setup or filled position is supplied.',
        'The packet contains one concrete setup; no filled position is supplied.'
    ).replace(
        'Explain room/obstacles for 2R versus 3R qualitatively; without entry and SL do not select a new target.',
        'Compare the supplied entry, structural SL, planned target and alternative 2R/3R '
        'with actual liquidity obstacles in the evidence. Do not move these levels or invent a new trade.'
    ).replace(
        'You do not replace those rules or certify a specific entry.',
        'You do not replace those rules. Assess this specific setup in its current market context.'
    ) + '''
This is an observational review of packet.setup only. For its direction, ALLOW means the
specific setup has contextual support at the supplied cutoff, WAIT means confirmation is
missing, and VETO means specific contradictory evidence. The other directional scenario is
background only and cannot authorize another trade. Identify evidence for/against this entry,
liquidity already swept, remaining liquidity targets and obstacles before 2R/3R. A high-timeframe
trend alone is insufficient to reject a reversal. State when history is insufficient.
Your answer is not an executable order. Do not claim that a delayed order would fill or that
the setup remains valid after your cutoff. The application separately records response
latency and expiry. Do not change quantity, stop, target or strategy.'''
    # Kept below as a separate extension so the complete production instruction
    # and its version can be exported without reconstructing a historical call.


def ict_prompt(base):
    return prompt(base) + '''
ICT review version directional-ict-setup-v2. Assess EVERY ict_context topic.
Return setup_binding with the exact order_id, direction and setup_fingerprint.
For each topic return SUPPORTS, CONTRADICTS, MIXED or UNKNOWN, evidence IDs,
reason_pl and missing_evidence. UNKNOWN is required when evidence is missing;
never fill a gap with remembered prices or events. Frozen detector labels are
observations to verify, not a trader-approved narrative. H1/H4 direction alone
cannot prohibit a reversal. Use a cited dealing range before asserting premium
or discount. A selected session range is not automatically that dealing range.
Distinguish sell-side/buy-side sweep and reclaim from directional breakout;
distinguish BOS from MSS, gap formation from mitigation and IFVG inversion.
Evaluate the original entry location, nearest liquidity/HTF obstacles before
2R/3R, ATR completeness/usage, NY session and ES/NQ alignment. SMT requires
paired comparable swing references, not merely candle agreement. Never use
ATR efficiency alone as ICT confirmation. Missing decisive context warrants
WAIT rather than an invented confirmation. Copy no outcome or future evidence.
Use only existing evidence IDs; geometry and validity must stay unchanged.
Keep each topic reason concise (one or two sentences); cite only decisive evidence IDs.
'''


def response_schema(base):
    value = copy.deepcopy(base)
    def obj(fields):
        return dict(type='object',properties=fields,required=list(fields),additionalProperties=False)
    string=dict(type='string'); ids=dict(type='array',items=string)
    value['properties']['setup_binding']=obj(dict(order_id=string,
        direction=dict(type='string',enum=['LONG','SHORT']),setup_fingerprint=string))
    assessment=obj(dict(assessment=dict(type='string',enum=['SUPPORTS','CONTRADICTS','MIXED','UNKNOWN']),
                        evidence_ids=ids,reason_pl=string,missing_evidence=ids))
    value['properties']['ict_assessment']=obj({name:assessment for name in directional_ict_context.TOPICS})
    value['required'] += ['setup_binding','ict_assessment']
    return value


def validate_binding(decision, packet):
    expected=dict(order_id=packet['setup']['order_id'],direction=packet['setup']['direction'],
                  setup_fingerprint=packet['setup_fingerprint'])
    if decision['setup_binding'] != expected:
        raise ValueError('ai_setup_binding_mismatch')
    for item in decision['ict_assessment'].values():
        if any(ref not in packet['evidence'] for ref in item['evidence_ids']):
            raise ValueError('ict_evidence_missing')
        if not item['reason_pl'].strip():
            raise ValueError('ict_reason_required')
        if item['assessment'] != 'UNKNOWN' and not item['evidence_ids']:
            raise ValueError('ict_claim_without_evidence')
        if item['assessment'] == 'UNKNOWN' and not item['missing_evidence']:
            raise ValueError('ict_unknown_requires_missing_evidence')
