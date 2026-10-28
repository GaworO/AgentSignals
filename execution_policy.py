"""Account-local entry permissions; never gates protective cancel/flatten.

V3_ONLY permits Directional 2R or explicitly enabled Model 0 runner brackets.
Active exits have separate ownership, parity and execution gates.
It does not replace Guard, prove broker fills or change detector rules.
"""
import math
import os
import re
import execution_safety


def mode():
    value = os.environ.get('EXEC_STRATEGY_POLICY', 'LEGACY').strip().upper()
    return value if value in {'LEGACY', 'V3_ONLY', 'AB_ONLY', 'DIRECTIONAL_Q1_ONLY', 'SHADOW_ALL'} else 'INVALID'


def shadow_family(strategy):
    value = mode()
    return (value == 'SHADOW_ALL' or (value == 'V3_ONLY' and strategy != 'AB_DIRECTIONAL')
            or (value == 'AB_ONLY' and strategy != 'A/B')
            or (value == 'DIRECTIONAL_Q1_ONLY' and strategy not in ('AB_DIRECTIONAL', 'A/B')))


def signal_family(signal):
    if (signal.get('_v3_directional') is True
            and signal.get('model') == 'A/B Directional'
            and signal.get('_strat') == 'A/B Directional ' + str(signal.get('dir'))
            and signal.get('_continuation_order_id')):
        return 'AB_DIRECTIONAL'
    if signal.get('_v3_directional') or signal.get('model') == 'A/B Directional' or signal.get('strategy') == 'AB_DIRECTIONAL':
        return 'AB_DIRECTIONAL_INVALID'
    return str(signal.get('_strat') or 'A/B')


def entry_blocker(signal):
    safety = execution_safety.entry_blocker()
    if safety:
        return safety
    value = mode()
    if value == 'LEGACY':
        return None
    if value == 'INVALID':
        return 'execution_policy_invalid'
    if shadow_family(signal_family(signal)):
        return 'shadow_strategy:' + value
    if value == 'AB_ONLY':
        return ab_entry_blocker(signal)
    if value == 'DIRECTIONAL_Q1_ONLY' and signal_family(signal) == 'A/B':
        import ab_q1_execution
        order = signal.get('_q1_order')
        if not order or any(signal.get(k) != order.get(v) for k,v in (('dir','direction'),('entry','entry_price'),('SL','stop_price'),('TP','target_price'))):
            return 'q1_prepared_signal_required'
        return ab_q1_execution.permission(order)
    if os.environ.get('V3_ENTRY_ARMED', '0') != '1':
        return 'v3_entry_not_armed'
    if len(os.environ.get('GUARD_TOKEN', '')) < 32:
        return 'v3_guard_admin_token_required'
    ticker = os.environ.get('EXEC_TICKER', '').strip()
    if not re.fullmatch(r'MNQ[HMUZ](?:\d{2}|\d{4})', ticker):
        return 'v3_explicit_mnq_contract_required'
    if os.environ.get('AB_V3_CONTRACT', '').strip() != ticker:
        return 'v3_detector_broker_contract_mismatch'
    if os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED', '0') != '1':
        return 'v3_detector_contract_unverified'
    if os.environ.get('AB_V3_EXCLUSIVE_ROUTE', '0') != '1':
        return 'v3_entry_route_unconfirmed'
    if os.environ.get('EXEC_FX', '0') == '1':
        return 'v3_futures_route_required'
    try:
        offset = float(os.environ.get('PRICE_OFFSET', '0'))
        pv = float(os.environ.get('POINT_VALUE', '2'))
        e, sl, tp = (float(signal[k]) for k in ('entry', 'SL', 'TP'))
        tick = float(os.environ.get('EXEC_TICK', '0.25'))
        if not all(math.isfinite(v) for v in (offset, pv, e, sl, tp, tick)):
            raise ValueError('nonfinite')
        if offset != 0 or pv != 2 or tick != 0.25:
            return 'v3_execution_geometry_config_mismatch'
        direction = signal.get('dir')
        risk = e-sl if direction == 'LONG' else sl-e
        is_runner = signal.get('_v3_runner') is True
        if is_runner and (os.environ.get('V3_RUNNER_ENABLED','false').lower() != 'true' or os.environ.get('AB_V3_MODE','SHADOW').upper() != 'LIVE'):
            return 'v3_runner_not_enabled'
        multiple = 3 if is_runner else 2
        expected = e+multiple*risk if direction == 'LONG' else e-multiple*risk
        if direction not in {'LONG', 'SHORT'} or risk <= 0 or min(e,sl,tp) <= 0:
            return 'v3_invalid_bracket'
        if abs(tp-expected) > 1e-6 or any(abs(v/tick-round(v/tick)) > 1e-6 for v in (e,sl,tp)):
            return 'v3_runner_3r_bracket_required' if is_runner else 'v3_fixed_2r_bracket_required'
        if signal.get('_strict_risk_budget') is not True or signal.get('_disable_partial') is not True:
            return 'v3_strict_single_bracket_required'
        budget = float(signal.get('_risk_budget_usd') or 0)
        ceiling = 250 if os.environ.get('ACCOUNT_PLAN') in {'builder50','rapid_eod50'} else 500
        if not math.isfinite(budget) or not 0 < budget <= ceiling:
            return 'v3_invalid_risk_budget'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'v3_invalid_bracket'
    return None


def ab_entry_blocker(signal):
    if os.environ.get('AB_ENTRY_ARMED', '0') != '1':
        return 'ab_entry_not_armed'
    if os.environ.get('BUILDER_ORDER_PROVIDER') != 'crosstrade':
        return 'ab_crosstrade_required'
    if os.environ.get('EXEC_FX', '0') == '1':
        return 'ab_futures_required'
    if os.environ.get('AB_V3_MODE', 'SHADOW').upper() != 'SHADOW':
        return 'ab_directional_must_be_shadow'
    if len(os.environ.get('GUARD_TOKEN', '')) < 32:
        return 'ab_guard_admin_token_required'
    if os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED') != '1':
        return 'ab_detector_contract_unverified'
    ticker = os.environ.get('EXEC_TICKER', '')
    if not re.fullmatch(r'MNQ[HMUZ]20\d{2}', ticker) or os.environ.get('AB_V3_CONTRACT') != ticker:
        return 'ab_explicit_matching_contract_required'
    try:
        e, sl, tp = (float(signal[k]) for k in ('entry', 'SL', 'TP'))
        z = 1 if signal['dir'] == 'LONG' else -1
        if (signal['dir'] not in ('LONG', 'SHORT') or z*(e-sl) <= 0 or z*(tp-e) <= 0
                or any(not math.isfinite(v) or v <= 0 for v in (e, sl, tp))):
            return 'ab_invalid_bracket'
        # ENTRY_OFFSET_PTS is already applied to the canonical signal before
        # alerts, Guard and this adapter; preserve that existing strategy input.
        if (float(os.environ.get('PRICE_OFFSET','0')) != 0
                or float(os.environ.get('POINT_VALUE','2')) != 2 or float(os.environ.get('EXEC_TICK','0.25')) != .25):
            return 'ab_execution_geometry_config_mismatch'
    except (ValueError, KeyError, TypeError, OverflowError):
        return 'ab_invalid_bracket'
    return None


def status():
    import execution_validation
    return dict(policy=mode(), q1_execution=__import__('ab_q1_execution').status(), live_execution_enabled=execution_safety.writes_enabled(),
                revalidation=execution_validation.status(),
                execution_kill_switch=os.environ.get('EXEC_KILL_SWITCH','false') != 'false',
                entry_armed=os.environ.get('AB_ENTRY_ARMED' if mode() == 'AB_ONLY' else 'V3_ENTRY_ARMED', '0') == '1',
                entry_variant=('CANONICAL_AB_NATIVE_BRACKET' if mode() == 'AB_ONLY' else 'DIRECTIONAL_MODEL0_RUNNER_2R_3R' if os.environ.get('V3_RUNNER_ENABLED','false').lower()=='true' and os.environ.get('AB_V3_MODE','SHADOW').upper()=='LIVE' else 'DIRECTIONAL_FIXED_SL_TP_2R'),
                active_manager_live=(os.environ.get('AB_MANAGER_MODE','off').lower()=='real' if mode()=='AB_ONLY' else os.environ.get('AB_V3_MODE','SHADOW').upper()=='LIVE' and os.environ.get('V3_EXIT_MODE','shadow').lower()=='real'),
                broker_fill_proven_by_http=False,
                note='Entry permission; Guard, broker ownership and active exit readiness are checked separately.')


def directional_health():
    """Static execution blockers for Guard's display, not order authorization."""
    if mode() not in ('V3_ONLY','DIRECTIONAL_Q1_ONLY'):return None
    s=status();revalidation=s['revalidation'];reasons=[]
    if not s['live_execution_enabled']:reasons.append('live_execution_disabled')
    if s['execution_kill_switch']:reasons.append('execution_kill_switch')
    if not s['entry_armed']:reasons.append('v3_entry_not_armed')
    if os.environ.get('AB_V3_MODE','SHADOW').upper()!='LIVE':reasons.append('directional_mode_not_live')
    directions=[side for side in ('LONG','SHORT') if os.environ.get('AB_DIRECTIONAL_LIVE_'+side,'0')=='1']
    if not directions:reasons.append('direction_not_enabled')
    if revalidation.get('policy_error'):
        reasons.append(revalidation.get('policy_detail') or revalidation['policy_error'])
    for blocker in revalidation.get('access_blockers') or []:
        # This is the same dependency, not evidence of an unprotected position.
        if blocker=='native_bracket_execution_policy_unavailable' and revalidation.get('policy_error'):
            continue
        reasons.append(blocker)
    reasons.extend('missing_validator:'+x for x in (revalidation.get('missing_validators') or []))
    if revalidation.get('ready') is not True and not reasons:reasons.append('execution_revalidation_not_ready')
    return dict(ready=not reasons,blockers=list(dict.fromkeys(reasons)),enabled_directions=directions,
                note='Static readiness only; each order still requires current AI, broker, quote and risk checks.')
