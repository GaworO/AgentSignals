"""Original A/B only: confirmed native fills, frozen M3, bounded Model 0 runner.

AI can permit a 3R attempt; it cannot create an entry or independently close.
A resting SL/TP is always submitted with entry. Market exits use current broker
ownership, never a simulated M1 touch as proof of a fill or realized 2R profit.
"""
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import v3_frozen_exit as frozen
import v3_runner as runner

POLICY = 'AB_M3_MODEL0_CONTEXT_V1'
LIFECYCLE = 'AB_NATIVE_MANAGED_V1'
FROZEN_HASH = 'f2349c4f979f905641002fa0356b7c8ea3239aa2088182701c0a3b9f9cfeb0b9'


def mode():
    value = os.environ.get('AB_MANAGER_MODE', 'off').lower()
    return value if value in ('off', 'shadow', 'real') else 'invalid'


def readiness(at_ms=None):
    if mode() == 'invalid':
        return 'ab_manager_invalid_mode'
    try:
        digest = hashlib.sha256(Path(frozen.__file__).read_bytes()).hexdigest()
    except OSError:
        return 'ab_manager_m3_unavailable'
    if digest != FROZEN_HASH:
        return 'ab_manager_m3_hash_mismatch'
    if not runner.model_ok(at_ms):
        return 'ab_manager_runner_model_invalid'
    return None


def plan_signal(signal, context, now):
    """Freeze the executable bracket before alert, sizing and Guard see it."""
    import execution_policy
    if execution_policy.mode() != 'AB_ONLY' or execution_policy.signal_family(signal) != 'A/B' or mode() != 'real':
        return
    if signal.get('_ab_manager'):
        return
    error = readiness(int(now * 1000))
    if error:
        raise ValueError(error)
    z = 1 if signal['dir'] == 'LONG' else -1
    entry, stop = (round(float(signal[k]) * 4) / 4 for k in ('entry', 'SL'))
    risk = z * (entry - stop)
    if risk <= 0:
        raise ValueError('ab_manager_invalid_risk')
    allowed = context_allows(context, now)
    plan = dict(policy=POLICY, lifecycle=LIFECYCLE, original_target=signal['TP'],
                baseline_target_price=entry + z * 2 * risk, runner_enabled=allowed,
                context_review_id=context.get('review_id'), context_state=context.get('state'),
                context_expires_at=context.get('expires_at'), planned_at=now,
                reason='AI_ALLOW_3R_ATTEMPT' if allowed else 'CONTEXT_NOT_ALLOW_FIXED_2R')
    signal.update(TP=entry + z * (3 if allowed else 2) * risk, _ab_manager=plan)


def context_allows(value, now):
    try:
        return (value.get('owner') == 'A/B' and value.get('state') == 'ALLOW'
                and bool(value.get('review_id'))
                and value['cutoff'] <= value['available_at'] <= now < value['expires_at'])
    except (KeyError, TypeError):
        return False


def validate_plan(signal, now):
    if mode() == 'invalid':
        return 'ab_manager_invalid_mode'
    if mode() != 'real':
        return 'ab_manager_not_real' if signal.get('_ab_manager') else None
    error = readiness(int(now * 1000))
    if error:
        return error
    plan = signal.get('_ab_manager') or {}
    if plan.get('policy') != POLICY or plan.get('lifecycle') != LIFECYCLE:
        return 'ab_manager_plan_missing'
    if plan.get('runner_enabled') and not now < (plan.get('context_expires_at') or 0):
        return 'ab_manager_context_expired_before_entry'
    z = 1 if signal['dir'] == 'LONG' else -1
    entry, stop, target = (round(float(signal[k]) * 4) / 4 for k in ('entry', 'SL', 'TP'))
    risk = z * (entry - stop)
    if (risk <= 0 or plan.get('baseline_target_price') != entry + z * 2 * risk
            or target != entry + z * (3 if plan.get('runner_enabled') else 2) * risk):
        return 'ab_manager_plan_geometry_mismatch'
    return None


def tables(c):
    c.execute('CREATE TABLE IF NOT EXISTS ab_manager_reviews(order_id TEXT,known_ms INTEGER,payload_json TEXT,PRIMARY KEY(order_id,known_ms))')
    c.execute('CREATE TABLE IF NOT EXISTS ab_manager_closes(order_id TEXT PRIMARY KEY,action_id TEXT,state TEXT,payload_json TEXT,updated_ms INTEGER)')


def assess(order, position, minutes, known, now, context, previous_runner=None):
    """Pure causal decision; input errors leave the native bracket in control."""
    result = dict(policy=POLICY, decision='HOLD', reason='M3_NO_EXIT', known_ms=known,
                  evaluated_ms=now, context_state=context.get('state'), runner=previous_runner)
    if readiness(now):
        return dict(result, reason='MODEL_UNAVAILABLE')
    if not 0 <= now - known <= 15000:
        return dict(result, reason='M1_STALE')
    fill_bar = int(position['fill_ms']) // 60000 * 60000
    full = minutes[(minutes.index > pd.Timestamp(fill_bar, unit='ms', tz='UTC')) &
                   (minutes.index < pd.Timestamp(known, unit='ms', tz='UTC'))]
    if full.empty:
        return dict(result, reason='WAIT_FULL_POSTFILL_M1')
    z = 1 if order['direction'] == 'LONG' else -1
    favorable = full['high' if z == 1 else 'low']
    adverse = full['low' if z == 1 else 'high']
    if ((z * (adverse - order['stop_price']) <= 0).any()
            or (z * (favorable - order['target_price']) >= 0).any()):
        return dict(result, reason='BRACKET_TOUCHED_WAIT_BROKER')
    touches = full[z * (favorable - order['baseline_target_price']) >= 0]
    if len(touches):
        if not order.get('runner_enabled'):
            return dict(result, reason='FIXED_2R_WAIT_BROKER')
        if previous_runner:
            return dict(result, reason='RUNNER_DECISION_ALREADY_FROZEN')
        touch = int(touches.index[0].value // 1000000)
        try:
            if position['entry_price'] != order['entry_price']:
                raise ValueError('actual_fill_geometry_changed')
            picked = runner.decide(order, position, minutes[minutes.index < touches.index[0]], touch)
            allowed = context_allows(context, now / 1000)
            selected = picked['selected'] and allowed
            decision = dict(picked, selected=selected, model_selected=picked['selected'],
                            context_allowed=allowed, context_review_id=context.get('review_id'),
                            touch_bar_ms=touch, decided_at_ms=now)
            reason = 'MODEL0_AND_CONTEXT_HOLD_3R' if selected else 'MODEL0_OR_CONTEXT_EXIT_AFTER_2R'
        except (ValueError, KeyError, TypeError, OSError):
            selected = False
            decision = dict(selected=False, error='RUNNER_INPUT_UNAVAILABLE', touch_bar_ms=touch, decided_at_ms=now)
            reason = 'RUNNER_DATA_FALLBACK_EXIT_AFTER_2R'
        return dict(result, decision='HOLD' if selected else 'CLOSE', reason=reason, runner=decision)
    # No hourly AI close: early exits require a current frozen M3 event.
    try:
        state = frozen.evaluate(order, position, minutes, known, dict(M1=False, M2=False, M3=True))
        signals = state['signals']
        if signals.get('primary_manager') == 'M3':
            return dict(result, decision='CLOSE', reason=signals['primary_reason'], m3=signals)
    except (ValueError, KeyError, TypeError, IndexError):
        return dict(result, reason='M3_INPUT_UNAVAILABLE')
    return result


def monitor(api=None, context_reader=None, closer=None):
    if mode() not in ('shadow', 'real'):
        return
    if api is None:
        import ab_v3_live as api
    if context_reader is None:
        import ab_ai_context
        context_reader = ab_ai_context.current
    if closer is None:
        import builder_crosstrade
        closer = builder_crosstrade.close_owned_position
    with api.connect() as c:
        tables(c)
        rows = c.execute("SELECT * FROM orders WHERE state='BROKER_CONFIRMED_OPEN'").fetchall()
    for row in rows:
        order = json.loads(row['order_json']); position = json.loads(row['position_json'] or '{}')
        if order.get('strategy') != 'A/B' or order.get('lifecycle') != LIFECYCLE:
            continue
        now = api.now_ms()
        if (position.get('assumed') or not position.get('exclusive') or not position.get('protection_verified')
                or position.get('quantity') != order['quantity']
                or any(position.get(k) != order[k] for k in ('contract', 'route_id', 'account_label', 'direction'))
                or not 0 <= now - position.get('event_ms', 0) <= 45000):
            continue
        with api.connect() as c:
            if c.execute('SELECT 1 FROM ab_manager_closes WHERE order_id=?', (row['order_id'],)).fetchone():
                continue
            last = c.execute("SELECT MAX(ts_ms) FROM bars WHERE tf='M1' AND contract=? AND ts_ms+60000<=?", (order['contract'], now)).fetchone()[0]
            if last is None:
                continue
            known = int(last) + 60000
            if c.execute('SELECT 1 FROM ab_manager_reviews WHERE order_id=? AND known_ms=?', (row['order_id'], known)).fetchone():
                continue
            prior = c.execute('SELECT payload_json FROM ab_manager_reviews WHERE order_id=? ORDER BY known_ms DESC LIMIT 1', (row['order_id'],)).fetchone()
            previous_runner = json.loads(prior[0]).get('runner') if prior else None
            # Include market warmup only; the frozen evaluator discards future bars.
            minutes = api.frame(c, 'M1', order['contract'], position['fill_ms'] // 60000 * 60000 - 60 * 60000, known)
        context = context_reader(dict(dir=order['direction'])) if order.get('runner_enabled') else {}
        decision = assess(order, position, minutes, known, now, context, previous_runner)
        aid = 'ab-manager-close-' + row['order_id']
        dispatch = False
        with api.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            fresh = c.execute('SELECT state,position_json FROM orders WHERE order_id=?', (row['order_id'],)).fetchone()
            if not fresh or fresh['state'] != 'BROKER_CONFIRMED_OPEN' or fresh['position_json'] != row['position_json']:
                continue
            inserted = c.execute('INSERT OR IGNORE INTO ab_manager_reviews VALUES(?,?,?)', (row['order_id'], known, api.dump(decision))).rowcount
            if (inserted and decision['decision'] == 'CLOSE' and mode() == 'real'
                    and 0 <= api.now_ms() - known <= 15000):
                dispatch = bool(c.execute('INSERT OR IGNORE INTO ab_manager_closes VALUES(?,?,?,?,?)',
                    (row['order_id'], aid, 'RESERVED', api.dump(decision), api.now_ms())).rowcount)
                if dispatch:
                    c.execute('UPDATE exit_trades SET primary_manager=?,primary_reason=?,signal_time=?,requested_time=?,broker_request_id=?,exit_mode=? WHERE trade_id=?',
                        ('AB_MANAGER', decision['reason'], now, now, aid, 'real', row['order_id']))
        if dispatch:
            # Native action obtains a NEW broker snapshot, proves ownership and
            # durably reserves its HTTP mutation. Ambiguous outcomes never retry.
            reply = {}; state = 'UNKNOWN'
            try:
                reply = closer(row['order_id'], aid)
                state = 'ACKNOWLEDGED' if reply.get('success') else 'UNKNOWN'
            except Exception:
                pass
            with api.connect() as c:
                c.execute('UPDATE ab_manager_closes SET state=?,updated_ms=? WHERE order_id=?', (state, api.now_ms(), row['order_id']))
                c.execute('UPDATE exit_trades SET broker_order_id=?,broker_response_json=? WHERE trade_id=?',
                          (str(reply.get('response', {}).get('orderId') or ''), api.dump(reply), row['order_id']))
                api._exit_transition(c, row['order_id'], 'EXIT_ACKNOWLEDGED' if state == 'ACKNOWLEDGED' else 'EXIT_BLOCKED', api.now_ms(),
                                     None if state == 'ACKNOWLEDGED' else 'ab_manager_close_unconfirmed_no_retry')


def status(api=None):
    if api is None:
        import ab_v3_live as api
    with api.connect() as c:
        tables(c)
        reviews = [dict(r) for r in c.execute('SELECT * FROM ab_manager_reviews ORDER BY known_ms DESC LIMIT 20')]
        actions = [dict(r) for r in c.execute('SELECT order_id,state,updated_ms FROM ab_manager_closes ORDER BY updated_ms DESC LIMIT 20')]
    for row in reviews:
        row['decision'] = json.loads(row.pop('payload_json'))
    return dict(mode=mode(), policy=POLICY, readiness=readiness(api.now_ms()), reviews=reviews, actions=actions,
                timing='Completed M1; actual market fills, no guaranteed 2R execution',
                validation_scope='M3 and Model0 historical evidence is Directional; original A/B combination is new')
