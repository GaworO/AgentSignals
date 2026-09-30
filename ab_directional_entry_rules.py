"""Fixed pre-order V3 entry gates. No state, network, execution or exit policy."""
from __future__ import annotations

import json
import math

VERSION = "AB_DIRECTIONAL_NO_DIB_BOS50_V1"
MIN_BODY_FRACTION = 0.50


def _integer(value):
    if isinstance(value, bool):
        raise ValueError("boolean timestamp")
    result = int(value)
    if result != float(value):
        raise ValueError("nonintegral timestamp")
    return result


def evaluate(category, direction, ohlc, bos_ms, activation_ms):
    """Use only the identified, completed BOS candle, symmetrically by side.

    DIB is an excluded setup family, not disabled inside the detector. Keeping
    the detector unchanged prevents generating a replacement non-DIB chain.
    Returned evidence travels with the order and is checked again at dispatch.
    """
    result = dict(version=VERSION, category=category, direction=direction,
                  bos_ms=None, activation_ms=None, bos_available_ms=None,
                  bos_ohlc=None, bos_body_fraction=None,
                  min_bos_body_fraction=MIN_BODY_FRACTION,
                  eligible=False, rejection_reason=None)

    def reject(reason):
        result['rejection_reason'] = reason
        return result

    if not isinstance(category, str) or not category.strip():
        return reject('V3_SETUP_CATEGORY_MISSING')
    if 'DIB' in {part.strip().upper() for part in category.split('+')}:
        return reject('V3_DIB_DISABLED')
    if direction not in {'LONG', 'SHORT'}:
        return reject('V3_DIRECTION_INVALID')
    try:
        bos, activation = _integer(bos_ms), _integer(activation_ms)
        if bos < 0 or bos % 60000 or activation < 0:
            raise ValueError('invalid bar timestamp')
    except (ValueError, TypeError, OverflowError):
        return reject('V3_BOS_TIMESTAMP_INVALID')
    result.update(bos_ms=bos, activation_ms=activation, bos_available_ms=bos + 60000)
    if bos + 60000 > activation:
        return reject('V3_BOS_NOT_CLOSED')
    try:
        values = {key: float(ohlc[key]) for key in ('open', 'high', 'low', 'close')}
        if not all(math.isfinite(x) for x in values.values()):
            raise ValueError('nonfinite OHLC')
        o, h, l, c = (values[k] for k in ('open', 'high', 'low', 'close'))
        if not 0 < l <= min(o, c) <= max(o, c) <= h or h <= l:
            raise ValueError('invalid OHLC geometry or zero range')
    except (KeyError, ValueError, TypeError, OverflowError):
        return reject('V3_BOS_OHLC_INVALID')
    z = 1 if direction == 'LONG' else -1
    fraction = z * (c - o) / (h - l)
    result.update(bos_ohlc=values, bos_body_fraction=fraction)
    if fraction < MIN_BODY_FRACTION:
        return reject('V3_BOS_BODY_BELOW_50PCT')
    result['eligible'] = True
    return result


def dispatch_blocker(order):
    """Old pending rows without the new evidence cannot bypass the new gates.

    No broker calls, cancellations or position management. Non-directional
    strategies continue through their existing execution path.
    """
    if str(order.get('strategy') or '').upper() != 'AB_DIRECTIONAL':
        return None
    try:
        payload = order.get('payload_json')
        payload = json.loads(payload) if payload else order
        if not isinstance(payload, dict):
            return 'v3_entry:missing_evidence'
        evidence = payload.get('entry_rules')
        if not isinstance(evidence, dict) or evidence.get('version') != VERSION:
            return 'v3_entry:missing_or_old_evidence'
        if evidence.get('direction') != order.get('direction'):
            return 'v3_entry:direction_mismatch'
        if evidence.get('category') != payload.get('setup_category'):
            return 'v3_entry:category_mismatch'
        if _integer(evidence.get('activation_ms')) != _integer(order.get('activation_ms')):
            return 'v3_entry:activation_mismatch'
        checked = evaluate(evidence.get('category'), order.get('direction'),
                           evidence.get('bos_ohlc'), evidence.get('bos_ms'),
                           order.get('activation_ms'))
        if not checked['eligible']:
            return 'v3_entry:' + checked['rejection_reason']
        if evidence.get('eligible') is not True:
            return 'v3_entry:evidence_rejected'
    except (ValueError, TypeError, KeyError, OverflowError):
        return 'v3_entry:invalid_evidence'
    return None
