"""Account-local entry permissions; never gates protective cancel/flatten.

V3_ONLY permits frozen Directional brackets, NOT active V3 manager exits.
It does not replace Guard, prove broker fills or change detector rules.
"""
import math
import os
import re


def mode():
    value = os.environ.get('EXEC_STRATEGY_POLICY', 'LEGACY').strip().upper()
    return value if value in {'LEGACY', 'V3_ONLY', 'SHADOW_ALL'} else 'INVALID'


def shadow_family(strategy):
    value = mode()
    return value == 'SHADOW_ALL' or (value == 'V3_ONLY' and strategy != 'AB_DIRECTIONAL')


def signal_family(signal):
    if (signal.get('_v3_directional') is True
            and signal.get('model') == 'A/B Directional'
            and signal.get('_strat') == 'A/B Directional ' + str(signal.get('dir'))
            and signal.get('_continuation_order_id')):
        return 'AB_DIRECTIONAL'
    return str(signal.get('_strat') or 'A/B')


def entry_blocker(signal):
    value = mode()
    if value == 'LEGACY':
        return None
    if value == 'INVALID':
        return 'execution_policy_invalid'
    if shadow_family(signal_family(signal)):
        return 'shadow_strategy:' + value
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
        expected = e+2*risk if direction == 'LONG' else e-2*risk
        if direction not in {'LONG', 'SHORT'} or risk <= 0 or min(e,sl,tp) <= 0:
            return 'v3_invalid_bracket'
        if abs(tp-expected) > 1e-6 or any(abs(v/tick-round(v/tick)) > 1e-6 for v in (e,sl,tp)):
            return 'v3_fixed_2r_bracket_required'
        if signal.get('_strict_risk_budget') is not True or signal.get('_disable_partial') is not True:
            return 'v3_strict_single_bracket_required'
        budget = float(signal.get('_risk_budget_usd') or 0)
        ceiling = 250 if os.environ.get('ACCOUNT_PLAN') in {'builder50','rapid_eod50'} else 500
        if not math.isfinite(budget) or not 0 < budget <= ceiling:
            return 'v3_invalid_risk_budget'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'v3_invalid_bracket'
    return None


def status():
    return dict(policy=mode(), entry_armed=os.environ.get('V3_ENTRY_ARMED', '0') == '1',
                entry_variant='DIRECTIONAL_FIXED_SL_TP_2R', active_manager_live=False,
                broker_fill_proven_by_http=False,
                note='Entry-only permission; Guard still decides. Active manager release remains blocked.')
