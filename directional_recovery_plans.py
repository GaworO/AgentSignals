"""Causal NO-FILL price plans; no model, broker or execution-permission writes.

The source candle is an explicit detector/operator association. fvg_bar (the
third candle confirming a gap) is NEVER implicitly used as its stop anchor.
Unknown association rejects a plan, rather than guessing the first/middle bar.
"""
import copy
import hashlib
import json
import math
import os
import re
from decimal import Decimal

VERSION = 'directional-recovery-wick-v1'
KINDS = ('FIBO_50_LIMIT', 'DEEP_FVG_LIMIT', 'NEW_BOS_FVG_RETEST')
GEOMETRY = ('order_id', 'direction', 'contract', 'entry_price', 'stop_price',
            'target_price', 'activation_ms', 'expiry_ms')
TICK = Decimal('0.25')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('NONFINITE_OR_MISSING_PRICE')
    return Decimal(str(value))


def on_tick(value):
    return number(value) % TICK == 0


def original_view(original):
    """Only causal geometry/anchors enter AI; never the broker or replay ledger."""
    keys = (*GEOMETRY, 'strategy', 'recovery_anchors')
    return {k:copy.deepcopy(original[k]) for k in keys if k in original}


def bar(packet, ref, contract):
    e = packet['evidence'].get(ref)
    if not isinstance(e, dict):
        raise ValueError('SOURCE_CANDLE_UNKNOWN')
    if e.get('kind') != 'bar' or e.get('symbol') != 'MNQ' or e.get('contract') != contract:
        raise ValueError('SOURCE_CANDLE_CONTRACT_UNVERIFIED')
    if type(e.get('timeframe')) is not int or e['timeframe'] <= 0:
        raise ValueError('CANDLE_TIMEFRAME_UNKNOWN')
    if (type(e.get('time')) is not int or type(e.get('available_at')) is not int
            or e['available_at'] != e['time'] + e['timeframe'] * 60
            or e['available_at'] > packet['as_of']):
        raise ValueError('CANDLE_NOT_CLOSED_AT_REVIEW')
    op, hi, lo, cl = (number(e.get(k)) for k in ('open', 'high', 'low', 'close'))
    if not 0 < lo <= min(op, cl) <= max(op, cl) <= hi:
        raise ValueError('CANDLE_GEOMETRY_INVALID')
    if any(v % TICK for v in (op, hi, lo, cl)):
        raise ValueError('CANDLE_OFF_MNQ_TICK')
    return e


def closed_path(packet, contract, start_ms):
    rows = sorted((e for e in packet['evidence'].values()
                   if e.get('kind') == 'bar' and e.get('symbol') == 'MNQ'
                   and e.get('contract') == contract and e.get('timeframe') == 1
                   and e.get('time', -1) >= start_ms // 1000), key=lambda e:e['time'])
    expected = list(range(start_ms // 1000, packet['as_of'] // 60 * 60, 60))
    if [e['time'] for e in rows] != expected:
        raise ValueError('POST_SETUP_HISTORY_INCOMPLETE')
    for e in rows:
        # Reuse full closed-bar/contract validation, including malformed OHLC.
        bar(dict(packet, evidence={'path':e}), 'path', contract)
    return rows


def original_valid(original, packet, now_ms):
    if original.get('strategy') != 'AB_DIRECTIONAL' or original.get('recovery_version'):
        raise ValueError('ORIGINAL_DIRECTIONAL_REQUIRED')
    if not re.fullmatch(r'MNQ[HMUZ]\d{4}', str(original.get('contract', ''))):
        raise ValueError('EXACT_MNQ_CONTRACT_REQUIRED')
    t0 = original['activation_ms']
    if type(t0) is not int or t0 % 60000 or original['expiry_ms'] != t0 + 600000:
        raise ValueError('ORIGINAL_VALIDITY_INVALID')
    if not t0 + 600000 <= now_ms < t0 + 1200000:
        raise ValueError('OUTSIDE_RECOVERY_WINDOW')
    if type(packet.get('as_of')) is not int or not 0 <= now_ms - packet['as_of'] * 1000 <= 120000:
        raise ValueError('RECOVERY_CONTEXT_STALE')
    if packet['as_of'] * 1000 < t0 + 600000:
        raise ValueError('TEN_MINUTE_CONTEXT_INCOMPLETE')
    sign = {'LONG':1, 'SHORT':-1}[original['direction']]
    entry, stop, target = (number(original[k]) for k in ('entry_price','stop_price','target_price'))
    if sign * (entry - stop) <= 0 or target != entry + 2 * (entry - stop):
        raise ValueError('ORIGINAL_GROSS_2R_GEOMETRY_REQUIRED')
    for e in closed_path(packet, original['contract'], t0):
        if (e['low'] <= stop or e['high'] >= target) if sign == 1 else (e['high'] >= stop or e['low'] <= target):
            raise ValueError('ORIGINAL_INVALIDATION_OR_TARGET_REACHED')


def build(original, packet, selection, now_ms, *, stop_buffer_points):
    """Return one immutable candidate, or an explicit UNKNOWN/BLOCK reason.

    Selection is prepared by deterministic code, never supplied by AI. It must
    contain gap evidence IDs and the explicit source_candle_id association.
    Buffer is supplied by the existing SL_ANCHOR_BUF configuration, not inferred
    from a model response. Off-tick midpoints remain blocked, never rounded.
    """
    result = dict(status='UNKNOWN', kind=selection.get('kind'), plan=None, reason=None)
    try:
        original_valid(original, packet, now_ms)
        kind = selection['kind']
        if kind not in KINDS:
            raise ValueError('RECOVERY_KIND_UNKNOWN')
        contract = original['contract']; sign = 1 if original['direction'] == 'LONG' else -1
        refs = selection.get('fvg_candle_ids')
        if not isinstance(refs, list) or len(refs) != 3 or len(set(refs)) != 3:
            raise ValueError('FVG_THREE_CANDLE_PROOF_REQUIRED')
        a, b, c = [bar(packet, ref, contract) for ref in refs]
        if (a['timeframe'] != b['timeframe'] or a['timeframe'] != c['timeframe']
                or b['time'] != a['available_at'] or c['time'] != b['available_at']):
            raise ValueError('FVG_CANDLES_NOT_CONTIGUOUS')
        low, high = (number(a['high']), number(c['low'])) if sign == 1 else (number(c['high']), number(a['low']))
        if low >= high:
            raise ValueError('DIRECTIONAL_FVG_NOT_PRESENT')
        anchor = selection.get('source_candle_id')
        if anchor not in refs or not selection.get('source_association_reference'):
            raise ValueError('FVG_SOURCE_CANDLE_ASSOCIATION_UNKNOWN')
        source = bar(packet, anchor, contract)
        buffer = number(stop_buffer_points)
        if buffer <= 0 or buffer % TICK:
            raise ValueError('WICK_BUFFER_INVALID')
        wick = number(source['low' if sign == 1 else 'high'])
        stop = wick - sign * buffer
        all_refs = list(refs)
        if kind == 'FIBO_50_LIMIT':
            anchors = original.get('recovery_anchors')
            if not isinstance(anchors, dict) or anchors.get('order_id') != original['order_id']:
                raise ValueError('ORIGINAL_FIBO_ANCHORS_UNKNOWN')
            values = []
            for name in ('origin', 'extreme'):
                item = anchors[name]; e = bar(packet, item['candle_id'], contract)
                field = ('low' if sign == 1 else 'high') if name == 'origin' else ('high' if sign == 1 else 'low')
                if e['available_at'] * 1000 > original['activation_ms'] or item['price'] != e[field]:
                    raise ValueError('ORIGINAL_FIBO_ANCHOR_CHANGED')
                values.append(number(item['price'])); all_refs.append(item['candle_id'])
            if sign * (values[1] - values[0]) <= 0:
                raise ValueError('ORIGINAL_FIBO_IMPULSE_INVALID')
            entry = sum(values) / 2  # Exact old anchors; intentionally no 1-point offset.
        else:
            entry = (low + high) / 2
        if kind == 'DEEP_FVG_LIMIT' and sign * (number(original['entry_price']) - entry) <= 0:
            raise ValueError('FVG_NOT_DEEPER_THAN_ORIGINAL_ENTRY')
        if kind == 'NEW_BOS_FVG_RETEST':
            event_id = selection.get('structure_event_id'); event = packet['evidence'].get(event_id, {})
            if (event.get('kind') not in ('BOS', 'MSS') or event.get('contract') != contract
                    or event.get('direction') != original['direction']
                    or event.get('source') != 'ORIGINAL_DIRECTIONAL_SCANNER'
                    or not original['activation_ms']/1000 < event.get('available_at', 0) <= c['available_at']
                    or event.get('available_at', math.inf) > packet['as_of']
                    or event.get('fvg_candle_ids') != refs or event.get('structural_source_candle_id') != anchor
                    or event.get('displacement_confirmed') is not True):
                raise ValueError('NEW_BOS_DISPLACEMENT_SWING_PROOF_UNKNOWN')
            all_refs.append(event_id)
        distance = sign * (entry - stop)
        if distance <= 0:
            raise ValueError('SOURCE_WICK_WRONG_SIDE_OF_ENTRY')
        if distance < 10:  # Existing Directional dispatch filter, preserved.
            raise ValueError('V3_INITIAL_STOP_LT_10_POINTS')
        target = entry + 2 * (entry - stop)
        if any(n % TICK for n in (entry, stop, target)):
            raise ValueError('EXACT_MIDPOINT_OFF_TICK')
        for e in closed_path(packet, contract, c['available_at'] * 1000):
            if e['close'] < low if sign == 1 else e['close'] > high:
                raise ValueError('SELECTED_FVG_INVERTED')
        for e in closed_path(packet, contract, original['activation_ms']):
            if e['low'] <= stop if sign == 1 else e['high'] >= stop:
                raise ValueError('SELECTED_WICK_INVALIDATION_ALREADY_REACHED')
        plan = dict(version=VERSION, opportunity_id=original['order_id'], plan_version=2,
                    kind=kind, strategy='AB_DIRECTIONAL', direction=original['direction'], contract=contract,
                    entry_price=float(entry), stop_price=float(stop), target_price=float(target),
                    stop_distance_points=float(distance), stop_risk_usd_per_contract=float(distance*2),
                    gross_r=2., activation_ms=now_ms, expiry_ms=original['activation_ms']+1200000,
                    fvg=dict(lower=float(low), upper=float(high), candle_ids=refs, available_at=c['available_at']),
                    stop_anchor=dict(candle_id=anchor, wick_field='low' if sign==1 else 'high',
                        wick_price=float(wick), buffer_points=float(buffer),
                        candle=copy.deepcopy(source), association_reference=selection['source_association_reference']),
                    evidence={k:copy.deepcopy(packet['evidence'][k]) for k in dict.fromkeys(all_refs)},
                    original_fingerprint=digest(original_view(original)), selection=copy.deepcopy(selection))
        plan['plan_id'] = digest(plan)
        plan['order_id'] = original['order_id'] + ':recovery:2:' + plan['plan_id'][:16]
        return dict(result, status='ELIGIBLE_FOR_AI', plan=plan)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return dict(result, reason=str(exc) if isinstance(exc, ValueError) else 'RECOVERY_EVIDENCE_INCOMPLETE')


def verify_plan(plan):
    content = {k:v for k,v in plan.items() if k not in ('plan_id','order_id')}
    if plan.get('version') != VERSION or plan.get('plan_id') != digest(content):
        raise ValueError('RECOVERY_PLAN_CHANGED')
    if plan['order_id'] != plan['opportunity_id'] + ':recovery:2:' + plan['plan_id'][:16]:
        raise ValueError('RECOVERY_ORDER_ID_CHANGED')


def size(plan, snapshot, binding, now_ms, cost_per_contract, legacy_budget):
    """Use the existing reconciled account/0.5% sizing engine unchanged."""
    import directional_account_limits
    verify_plan(plan)
    if not plan['activation_ms'] <= now_ms < plan['expiry_ms']:
        return dict(allowed=False, max_quantity=0, reason='RECOVERY_EXPIRED')
    try:
        if (not directional_account_limits.equity_percent_mode()
                or Decimal(os.environ.get('AB_DIRECTIONAL_RISK_PCT','0')) != Decimal('0.5')):
            raise ValueError('risk_mode')
    except (ValueError, ArithmeticError):
        return dict(allowed=False, max_quantity=0, reason='RECOVERY_05_PERCENT_NOT_CONFIGURED')
    return directional_account_limits.capacity(snapshot, binding, plan, now_ms,
                                                cost_per_contract, legacy_budget)
