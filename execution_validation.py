"""Final Directional gate, using separately supplied server-side evidence.

There is deliberately no candle-to-quote fallback. CrossTrade Tradovate has no
bid/ask API. Production stays blocked until a verified market adapter, a fresh
thesis validator and an account/risk validator are registered by the application.
Policy values must come from an explicitly documented and approved strategy
validation; none are inferred from observed trades or the AI response.
"""
import json
import math
import os
import time
from pathlib import Path

import execution_trace

_SOURCES = {}
LIMITS = ('max_signal_age_ms', 'max_quote_age_ms', 'entry_zone_points',
          'max_spread_ticks', 'max_slippage_ticks', 'max_volatility_points',
          'min_rr')


def configure_sources(*, market, thesis, risk):
    if not all(callable(f) for f in (market, thesis, risk)):
        raise ValueError('server_side_validators_required')
    _SOURCES.update(market=market, thesis=thesis, risk=risk)


def policy():
    path = os.environ.get('DIRECTIONAL_EXECUTION_POLICY_FILE')
    if not path:
        raise ValueError('validated_execution_policy_missing')
    value = json.loads(Path(path).read_text())
    if (value.get('approved') is not True or not value.get('validation_reference')
            or value.get('strategy') != 'AB_DIRECTIONAL'
            or not value.get('volatility_definition')):
        raise ValueError('validated_execution_policy_missing')
    for key in LIMITS:
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise ValueError('execution_parameter_missing:' + key)
    if value['min_rr'] < 2:
        raise ValueError('directional_minimum_2r_required')
    return value


def evaluate(order, quote, thesis, risk, review, parameters, now_ms):
    """Pure validation, also exercised by the offline broker simulator."""
    try:
        sign = {'LONG': 1, 'SHORT': -1}[order['direction']]
        entry, stop, target = (float(order[k]) for k in ('entry_price', 'stop_price', 'target_price'))
        tick = .25  # This adapter only supports the validated MNQ contract.
        contract = order['contract']
        if not (order['activation_ms'] <= now_ms < order['expiry_ms']):
            return 'signal_expired'
        if now_ms-order['activation_ms'] > parameters['max_signal_age_ms']:
            return 'signal_too_old'
        if quote.get('contract') != contract or parameters.get('contract') != contract:
            return 'contract_mismatch'
        if quote.get('market_open') is not True:
            return 'market_closed_or_unknown'
        if quote.get('source') != parameters.get('quote_source') or not quote.get('source'):
            return 'quote_source_unverified'
        if not 0 <= now_ms-quote['as_of_ms'] <= parameters['max_quote_age_ms']:
            return 'quote_stale_or_future'
        bid, ask, volatility = (float(quote[k]) for k in ('bid', 'ask', 'volatility_points'))
        if (not all(math.isfinite(v) and v > 0 for v in (entry, stop, target, bid, ask))
                or not math.isfinite(volatility) or volatility < 0 or bid > ask):
            return 'invalid_market_or_bracket'
        if any(abs(v/tick-round(v/tick)) > 1e-8 for v in (entry, stop, target, bid, ask)):
            return 'off_tick_price'
        px = ask if sign == 1 else bid
        if abs(px-entry) > parameters['entry_zone_points']:
            return 'outside_validated_entry_zone'
        if (ask-bid)/tick > parameters['max_spread_ticks']:
            return 'spread_limit'
        if max(0, sign*(px-entry))/tick > parameters['max_slippage_ticks']:
            return 'slippage_limit'
        if (quote.get('volatility_definition') != parameters['volatility_definition']
                or volatility > parameters['max_volatility_points']):
            return 'volatility_limit_or_definition'
        # The order is a LIMIT; an adverse fill beyond entry is not authorized.
        # Test both the original bracket and the current executable side.
        for price in (entry, px):
            loss, reward = sign*(price-stop), sign*(target-price)
            if loss <= 0 or reward <= 0:
                return 'bracket_invalidated'
            if reward/loss + 1e-9 < parameters['min_rr']:
                return 'reward_risk_below_minimum'
        if (thesis.get('order_id') != order['order_id'] or thesis.get('valid') is not True
                or not thesis.get('evidence_id')
                or not 0 <= now_ms-thesis['checked_ms'] <= parameters['max_quote_age_ms']):
            return 'directional_thesis_not_revalidated'
        if (risk.get('allowed') is not True or risk.get('order_id') != order['order_id']
                or not risk.get('account_binding')
                or not 0 <= now_ms-risk['checked_ms'] <= parameters['max_quote_age_ms']):
            return 'account_risk_not_revalidated'
        if (review.get('state') != 'ALLOW' or not review.get('review_id')
                or review.get('order_id') != order['order_id']
                or not review['available_at']*1000 <= now_ms < review['expires_at']*1000):
            return 'setup_review_missing_or_expired'
        geometry = ('direction', 'contract', 'entry_price', 'stop_price', 'target_price', 'activation_ms', 'expiry_ms')
        if any(review.get('setup', {}).get(k) != order.get(k) for k in geometry):
            return 'setup_review_geometry_mismatch'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'revalidation_evidence_incomplete'
    return None


def check(order, review, *, clock=time.time):
    started = int(clock()*1000)
    evidence = {}
    try:
        parameters = policy()
        for name in ('market', 'thesis', 'risk'):
            if name not in _SOURCES:
                raise ValueError(name + '_validator_unavailable')
            evidence[name] = _SOURCES[name](dict(order))
        reason = evaluate(order, evidence['market'], evidence['thesis'], evidence['risk'],
                          review or {}, parameters, int(clock()*1000))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'revalidation_source_failed'
    # A durable log failure propagates and prevents submission.
    execution_trace.record(order['order_id'], 'execution_eligibility', started_ms=started,
                           reason=reason, allowed=reason is None)
    return reason


def status():
    try:
        policy()
        policy_error = None
    except Exception as exc:
        policy_error = str(exc) if isinstance(exc, ValueError) else 'execution_policy_unreadable'
    missing = [name for name in ('market', 'thesis', 'risk') if name not in _SOURCES]
    return dict(ready=policy_error is None and not missing, policy_error=policy_error,
                missing_validators=missing, candle_quote_fallback=False)
