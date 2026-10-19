"""Canonical A/B bracket preparation; no HTTP or inferred broker fills."""
import hashlib
import json
import math
import os

LIFECYCLE = 'AB_NATIVE_FIXED_BRACKET_V1'


def configure_signal(signal):
    """One original A/B bracket; siblings and partials remain outside this route."""
    ceiling = 250 if os.environ.get('ACCOUNT_PLAN') in ('builder50', 'rapid_eod50') else 500
    account = float(os.environ.get('ACCOUNT', '50000'))
    requested = account * float(os.environ.get('RISK_PCT', '0.5')) / 100
    budget = min(ceiling, requested)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError('ab_invalid_risk_budget')
    signal.update(_strict_risk_budget=True, _disable_partial=True,
                  _risk_budget_usd=budget, _planned_group_risk_usd=budget)
    return [signal]


def prepare(signal, payload, candidate_id, api):
    from execution_policy import entry_blocker, signal_family
    error = entry_blocker(signal)
    if error or signal_family(signal) != 'A/B':
        raise ValueError(error or 'ab_canonical_signal_required')
    now = api.now_ms()
    origin = int(signal['bos_ms'])
    expiry = origin + int(os.environ.get('EXEC_CANCEL_AFTER_SEC', '600')) * 1000
    if not origin <= now < expiry:
        raise ValueError('ab_signal_expired_or_future')
    quantity = payload['quantity']
    e, sl, tp = payload['limitPrice'], payload['stopLoss']['stopPrice'], payload['takeProfit']['limitPrice']
    if (type(quantity) is not int or quantity < 1 or quantity > int(os.environ.get('EXEC_MAX_QTY', '15'))
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 or v*4 != round(v*4) for v in (e, sl, tp))
            or payload['ticker'] != api.contract()
            or (e,sl,tp) != tuple(round(float(signal[k])*4)/4 for k in ('entry','SL','TP'))):
        raise ValueError('ab_canonical_bracket_mismatch')
    sign = 1 if signal['dir']=='LONG' else -1
    if sign*(e-sl) <= 0 or sign*(tp-e) <= 0:
        raise ValueError('ab_tick_rounded_geometry_invalid')
    per_contract = abs(e-sl)*2
    if quantity * per_contract > signal['_risk_budget_usd'] + 1e-6:
        raise ValueError('ab_risk_budget_exceeded')
    oid = 'ab-' + hashlib.sha256((api.route()+'|'+candidate_id).encode()).hexdigest()[:48]
    order = dict(order_id=oid, strategy='A/B', candidate_id=candidate_id,
                 lifecycle=LIFECYCLE, original_levels={k:signal[k] for k in ('entry','SL','TP')}, direction=signal['dir'], activation_ms=now, expiry_ms=expiry,
                 entry_price=e, stop_price=sl, target_price=tp, baseline_target_price=tp,
                 risk_points=abs(e-sl), quantity=quantity, runner_enabled=False,
                 contract=api.contract(), route_id=api.route(), account_label=os.environ.get('ACCOUNT_LABEL', 'account'))
    with api.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM orders WHERE order_id=?', (oid,)).fetchone():
            raise ValueError('ab_candidate_already_reserved')
        # Shadow detector PREPARED rows do not own the native route. Actual
        # native submissions, including unknown outcomes, do own it.
        if db.execute("SELECT 1 FROM native_orders WHERE state NOT IN ('NO_FILL','CLOSED')").fetchone():
            raise ValueError('ab_native_route_already_owned')
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?)', (oid, 'PREPARED', json.dumps(order), None, now))
    signal['_ab_native_order_id'] = oid
    return oid


def record_result(api, oid, result):
    state = ('SUBMISSION_UNKNOWN' if result.get('submission_unknown') else
             'WAITING_BROKER_FILL' if result.get('sent') else 'REJECTED')
    with api.connect() as db:
        db.execute("UPDATE orders SET state=?,updated_ms=? WHERE order_id=? AND state='PREPARED'",
                   (state, api.now_ms(), oid))
