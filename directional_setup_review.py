"""Causal setup descriptions for observational AI reviews; no execution API."""
import json
import math


def describe(order, contract):
    if order.get('strategy') != 'AB_DIRECTIONAL':
        raise ValueError('directional_setup_required')
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
                  strategy='AB_DIRECTIONAL', direction=direction, contract=contract,
                  activation_ms=activation, expiry_ms=expiry, **levels,
                  risk_points=risk, target_r=reward/risk,
                  alternative_2r=levels['entry_price']+sign*2*risk,
                  alternative_3r=levels['entry_price']+sign*3*risk,
                  source_level=snapshot.get('source_level'), fvg_edge=snapshot.get('fvg_edge'),
                  setup_category=payload.get('setup_category'), entry_rules=payload.get('entry_rules'))
    if len(json.dumps(result, allow_nan=False)) > 12000:
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
