"""Final Directional gate, using separately supplied server-side evidence.

There is deliberately no candle-to-quote fallback. CrossTrade collective
quotes are monitored but cannot authorize orders without source verification.
An explicitly approved RESTING_LIMIT_CLOSED_M1_V1 policy uses closed candles
without claiming executable quotes. Both policies still require account allowances,
complete current thesis evidence and approved execution parameters.
Policy values must come from an explicitly documented and approved strategy
validation; none are inferred from observed trades or the AI response.
"""
import json
import math
import os
import re
import time
from pathlib import Path

import execution_trace
import directional_resting_limit as resting

_SOURCES = {}
LIMITS = ('max_signal_age_ms', 'max_quote_age_ms', 'entry_zone_points',
          'max_spread_ticks', 'max_slippage_ticks', 'max_volatility_points',
          'max_account_age_ms', 'max_thesis_age_ms', 'min_rr')


def configure_sources(*, market, thesis, risk):
    if not all(callable(f) for f in (market, thesis, risk)):
        raise ValueError('server_side_validators_required')
    _SOURCES.update(market=market, thesis=thesis, risk=risk)


def register_source(name, validator):
    if name not in ('market', 'thesis', 'risk', 'pending') or not callable(validator):
        raise ValueError('server_side_validator_required')
    _SOURCES[name] = validator


def policy_for(order):
    if order.get('strategy') == 'A/B':
        import ab_q1_execution
        if not ab_q1_execution.is_order(order):
            raise ValueError('exact_q1_order_required')
        return policy('A/B')
    return policy()


def policy(strategy='AB_DIRECTIONAL'):
    path = os.environ.get('AB_Q1_EXECUTION_POLICY_FILE' if strategy == 'A/B' else 'DIRECTIONAL_EXECUTION_POLICY_FILE')
    if not path:
        raise ValueError('validated_execution_policy_missing')
    value = json.loads(Path(path).read_text())
    if (value.get('approved') is not True or not value.get('validation_reference')
            or value.get('strategy') != strategy
            or (value.get('execution_model') != resting.MODEL and not value.get('volatility_definition'))):
        raise ValueError('validated_execution_policy_missing')
    if strategy == 'A/B' and (value.get('quality') != 'Q1' or value.get('target_rule') != 'ORIGINAL_AB_TARGET' or value.get('execution_model') != resting.MODEL):
        raise ValueError('q1_original_bracket_policy_required')
    validate_parameters(value)
    costs(value)
    return value


def validate_parameters(parameters):
    model = parameters.get('execution_model', 'EXECUTABLE_BBO_V1')
    if model == resting.MODEL:
        resting.validate_policy(parameters)
    elif model == 'EXECUTABLE_BBO_V1':
        validate_quote_parameters(parameters)
        if parameters['max_account_age_ms'] > 30000:
            raise ValueError('account_age_exceeds_existing_sync_limit')
    else:
        raise ValueError('unsupported_execution_model')


def validate_quote_parameters(parameters):
    """No implicit units, non-finite thresholds or continuous contracts."""
    if not re.fullmatch(r'MNQ[HMUZ]20\d{2}', str(parameters.get('contract', ''))):
        raise ValueError('explicit_mnq_contract_required')
    for key in LIMITS:
        v = parameters.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise ValueError('execution_parameter_missing:' + key)
    if parameters['min_rr'] < 2:
        raise ValueError('directional_minimum_2r_required')
    latency = parameters.get('max_quote_latency_ms', parameters['max_quote_age_ms'])
    if type(latency) not in (int, float) or not math.isfinite(latency) or latency <= 0:
        raise ValueError('invalid_max_quote_latency_ms')
    expiry = parameters.get('contract_last_trade_ms')
    if (type(expiry) is not int or expiry <= 0 or not parameters.get('contract_reference')):
        raise ValueError('verified_contract_expiry_required')


def price_assessment(order, quote, parameters, now_ms):
    """Diagnostic prices are explicitly observational; never an authorization."""
    side = 'ask' if order.get('direction') == 'LONG' else 'bid'
    px = quote.get(side)
    verified = (quote.get('execution_eligible') is True
                and quote.get('timestamp_semantics_verified') is True
                and quote.get('source') == 'crosstrade_nt8'
                and type(quote.get('provenance_expires_at_ms')) is int
                and now_ms < quote['provenance_expires_at_ms'])
    result = dict(order_id=order.get('order_id'), order_type='LIMIT',
        original_limit=order.get('entry_price'), stop=order.get('stop_price'),
        target=order.get('target_price'), price_side=side, observed_side_price=px,
        executable_price=px if verified else None,
        observational_only=not verified,
        source=quote.get('source'), bid=quote.get('bid'), ask=quote.get('ask'),
        as_of_ms=quote.get('as_of_ms'), request_latency_ms=quote.get('request_latency_ms'))
    try:
        sign = {'LONG': 1, 'SHORT': -1}[order['direction']]
        result.update(quote_age_ms=now_ms-quote['as_of_ms'],
            spread_ticks=(quote['ask']-quote['bid'])/.25,
            entry_deviation_points=abs(px-order['entry_price']),
            adverse_deviation_ticks=max(0,sign*(px-order['entry_price']))/.25,
            original_limit_respected=sign*(px-order['entry_price']) <= 0,
            reward_risk_at_original_limit=reward_risk(order, parameters))
    except (KeyError, TypeError, ValueError):
        result['assessment_incomplete'] = True
    return result


def costs(parameters):
    """Per-contract round-trip costs; never infer commission from cash changes."""
    value = parameters.get('costs', {})
    if not value.get('validation_reference') or value.get('contract') != parameters.get('contract'):
        raise ValueError('verified_transaction_costs_required')
    for key in ('round_trip_usd', 'stop_slippage_ticks'):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError('verified_transaction_costs_required')
    return value


def reward_risk(order, parameters):
    """Unchanged limit, net reward / total loss including RT costs. Per MNQ."""
    sign={'LONG':1,'SHORT':-1}[order['direction']]
    entry,stop,target=(float(order[k]) for k in ('entry_price','stop_price','target_price'))
    fees=costs(parameters)
    risk=sign*(entry-stop)*2
    reward=sign*(target-entry)*2
    loss=risk+fees['round_trip_usd']+fees['stop_slippage_ticks']*.5
    net_reward=reward-fees['round_trip_usd']
    if risk<=0 or loss<=0:raise ValueError('bracket_invalidated')
    return dict(gross_rr=reward/risk,net_rr=net_reward/loss,
                gross_risk_usd=risk,gross_reward_usd=reward,
                net_loss_usd=loss,net_reward_usd=net_reward,
                round_trip_usd=fees['round_trip_usd'],stop_slippage_ticks=fees['stop_slippage_ticks'],
                basis='NET_REWARD_DIVIDED_BY_COST_INCLUSIVE_LOSS',
                policy='MINIMUM_GROSS_RR_NET_REPORTED')


def evaluate(order, quote, thesis, risk, review, parameters, now_ms):
    """Pure validation, also exercised by the offline broker simulator."""
    try:
        validate_parameters(parameters)
        candle_policy = parameters.get("execution_model") == resting.MODEL
        import ab_q1_execution
        q1 = ab_q1_execution.is_order(order)
        if (order.get('strategy') == 'A/B' and (not q1 or parameters.get('strategy') != 'A/B' or parameters.get('quality') != 'Q1')):
            return 'exact_q1_policy_required'
        if (not order.get('order_id') or (order.get('strategy') != 'AB_DIRECTIONAL' and not q1)
                or str(order.get('order_type', 'LIMIT')).upper() != 'LIMIT'):
            return 'exact_directional_limit_setup_required'
        sign = {'LONG': 1, 'SHORT': -1}[order['direction']]
        entry, stop, target = (float(order[k]) for k in ('entry_price', 'stop_price', 'target_price'))
        tick = .25  # This adapter only supports the validated MNQ contract.
        contract = order['contract']
        if not (order['activation_ms'] <= now_ms < order['expiry_ms']):
            return 'signal_expired'
        if now_ms-order['activation_ms'] > parameters['max_signal_age_ms']:
            return 'signal_too_old'
        if parameters.get('contract') != contract:
            return 'contract_mismatch'
        if now_ms >= parameters['contract_last_trade_ms']:
            return 'contract_expired'
        if candle_policy:
            if not all(math.isfinite(v) and v > 0 for v in (entry, stop, target)):
                return 'invalid_market_or_bracket'
            if any(abs(v/tick-round(v/tick)) > 1e-8 for v in (entry, stop, target)):
                return 'off_tick_price'
            if sign*(entry-stop) <= 0 or sign*(target-entry) <= 0:
                return 'bracket_invalidated'
            if not q1 and abs(sign*(target-entry)-2*sign*(entry-stop)) > 1e-8:
                return 'original_fixed_2r_required'
            reason = resting.observation_check(order, quote, parameters, now_ms)
            if reason:return reason
            if risk.get('account_binding') != parameters['account_binding']:
                return 'resting_limit_account_binding_mismatch'
            px = entry  # Worst permitted LIMIT; not a claimed live market price.
        else:
            if quote.get('contract') != contract:
                return 'contract_mismatch'
            if quote.get('market_open') is not True:
                return 'market_closed_or_unknown'
            if (quote.get('source') != 'crosstrade_nt8'
                    or quote.get('execution_eligible') is not True
                    or quote.get('timestamp_semantics_verified') is not True):
                return 'quote_source_unverified'
            if quote.get('source') != parameters.get('quote_source') or not quote.get('source'):
                return 'quote_source_unverified'
            if not 0 <= now_ms-quote['as_of_ms'] <= parameters['max_quote_age_ms']:
                return 'quote_stale_or_future'
            latency = quote.get('request_latency_ms')
            if (type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0
                    or latency > parameters.get('max_quote_latency_ms', parameters['max_quote_age_ms'])):
                return 'quote_request_latency_exceeded_or_unknown'
            proof_end = quote.get('provenance_expires_at_ms')
            if proof_end is not None and now_ms >= proof_end:
                return 'quote_verification_expired'
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
            if sign*(px-entry) > 1e-9:
                return 'original_limit_exceeded'
        # The order is a LIMIT; an adverse fill beyond entry is not authorized.
        # Test both the original bracket and the current executable side.
        for price in (entry, px):
            loss, reward = sign*(price-stop), sign*(target-price)
            if loss <= 0 or reward <= 0:
                return 'bracket_invalidated'
            if reward/loss + 1e-9 < parameters['min_rr']:
                return 'reward_risk_below_minimum'
        # Worst permitted LIMIT entry is the unchanged limit, not an assumed
        # favourable fill at a transient quote. Stop slippage is a validated
        # stress allowance, never a guaranteed maximum on a stop-market fill.
        rr=reward_risk(order,parameters)
        # User-confirmed policy (2026-10-22): minimum 2R is GROSS.
        # Net RR remains cost-inclusive evidence, not a second 2R threshold.
        # Keep the existing protection against a non-positive net target.
        if rr['net_reward_usd']<=0:
            return 'net_reward_non_positive'
        if (thesis.get('order_id') != order['order_id'] or thesis.get('valid') is not True
                or not thesis.get('evidence_id')
                or not 0 <= now_ms-thesis['checked_ms'] <= parameters['max_thesis_age_ms']):
            return 'directional_thesis_not_revalidated' + (':' + str(thesis['reason']) if thesis.get('reason') else '')
        if (risk.get('allowed') is not True or risk.get('order_id') != order['order_id']
                or not risk.get('account_binding')
                or not 0 <= now_ms-risk['checked_ms'] <= parameters['max_account_age_ms']):
            return 'account_risk_not_revalidated' + (':' + str(risk['reason']) if risk.get('reason') else '')
        if (review.get('state') != 'ALLOW' or not review.get('review_id')
                or review.get('order_id') != order['order_id']
                or not review['available_at']*1000 <= now_ms < review['expires_at']*1000):
            return 'setup_review_missing_or_expired'
        if review.get('setup', {}).get('order_id') != order['order_id']:
            return 'setup_review_geometry_mismatch'
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
        parameters = policy_for(order)
        evidence['reward_risk']=reward_risk(order,parameters)
        # Fetch executable quotes last, after the slower account/context reads.
        for name in ('risk', 'thesis', 'market'):
            if name not in _SOURCES:
                raise ValueError(name + '_validator_unavailable')
            evidence[name] = _SOURCES[name](dict(order))
        import ab_ai_context
        if (os.environ.get('AB_AI_TRIGGER') == 'HYBRID' or
                getattr(ab_ai_context.INSTANCE,'trigger',None) == 'HYBRID'):
            import directional_ai_review
            if order.get('strategy') == 'A/B':
                import ab_q1_review
                blocked, latest = ab_q1_review.check(order, ab_ai_context.INSTANCE)
            else:
                blocked, latest = directional_ai_review.check(order['direction'],order['order_id'])
            if blocked or not review or latest.get('review_id') != review.get('review_id'):
                raise ValueError(blocked or 'hybrid_review_changed')
            review = latest
        evidence['execution_model'] = parameters.get('execution_model', 'EXECUTABLE_BBO_V1')
        evidence['price_assessment'] = (resting.assessment(order, evidence['market'], parameters)
            if parameters.get('execution_model') == resting.MODEL else
            price_assessment(order, evidence['market'], parameters, int(clock()*1000)))
        reason = evaluate(order, evidence['market'], evidence['thesis'], evidence['risk'],
                          review or {}, parameters, int(clock()*1000))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'revalidation_source_failed'
    # Persist the exact server-side inputs used by the final gate. No raw model
    # packet, credentials or provider authentication are part of these adapters.
    execution_trace.record(order['order_id'], 'execution_validation_evidence',
                           order={k:order.get(k) for k in ('order_id','direction','contract','entry_price','stop_price','target_price','expiry_ms')},
                           evidence=evidence, review_id=(review or {}).get('review_id'),
                           rr_policy='MINIMUM_GROSS_RR_NET_REPORTED')
    # A durable log failure propagates and prevents submission.
    execution_trace.record(order['order_id'], 'execution_eligibility', started_ms=started,
                           reason=reason, allowed=reason is None)
    return reason


def status():
    configured_model = None
    configured = {}
    try:
        path = os.environ.get('DIRECTIONAL_EXECUTION_POLICY_FILE')
        configured = json.loads(Path(path).read_text()) if path else {}
        if isinstance(configured, dict):
            configured_model = configured.get('execution_model')
    except (OSError, ValueError, TypeError):
        pass
    try:
        parameters = policy()
        policy_error = None
    except Exception as exc:
        parameters = {}
        policy_error = str(exc) if isinstance(exc, ValueError) else 'execution_policy_unreadable'
    policy_detail = policy_error
    if (policy_error == 'validated_execution_policy_missing' and isinstance(configured, dict)
            and configured.get('execution_model') and configured.get('approved') is not True):
        policy_detail = 'execution policy loaded; technical verification incomplete (approved=false)'
    missing = [name for name in ('market', 'thesis', 'risk') if name not in _SOURCES]
    access=[]
    import ab_v3_live
    if ab_v3_live.native_fixed_bracket():
        blocker=ab_v3_live.native_bracket_readiness()
        if blocker:access.append(blocker)
    candle_policy = parameters.get('execution_model') == resting.MODEL
    # An unapproved candle policy is blocked, not a request to switch to BBO.
    # This affects diagnostics only; check()/policy() still reject it.
    blocked_candle_policy = policy_error is not None and configured_model == resting.MODEL
    if not candle_policy and not blocked_candle_policy and 'market' in _SOURCES and (os.environ.get('DIRECTIONAL_QUOTE_SOURCE')!='crosstrade_nt8'
            or not (os.environ.get('DIRECTIONAL_QUOTE_API_TOKEN') or os.environ.get('BUILDER_BROKER_API_TOKEN'))):
        access.append('executable_quote_access_not_configured')
    if 'risk' in _SOURCES and not os.environ.get('MFFU_RISK_EVIDENCE_FILE'):
        access.append('mffu_current_allowances_missing')
    from directional_market_data import QuoteClient
    try:
        last=None if candle_policy or blocked_candle_policy else QuoteClient().latest()
        if last and last.get('quote',{}).get('execution_eligible') is False:
            access.append('quote_source_suitability_unverified')
        quote_status=None if not last else {k:last.get(k) for k in ('observed_ms','observation_age_ms','reason','quote','request_latency_ms')}
    except Exception:
        quote_status={'reason':'quote_monitor_unavailable'}
    return dict(quote_monitor=quote_status, ready=policy_error is None and not missing and not access, policy_error=policy_error,
                policy_detail=policy_detail,
                access_blockers=access,
                missing_validators=missing, candle_quote_fallback=False,
                execution_model=parameters.get('execution_model', configured_model or 'EXECUTABLE_BBO_V1'),
                configured_execution_model=configured_model,
                policy_validated=policy_error is None,
                executable_quotes_required=None if blocked_candle_policy else not candle_policy,
                residual_risks=list(resting.RISKS) if candle_policy or blocked_candle_policy else [])


def configuration_files():
    """Read actual policy paths for diagnostics; loading is never approval."""
    import hashlib
    result={}
    for key,fields in {
        'DIRECTIONAL_EXECUTION_POLICY_FILE':('approved','execution_model','max_quantity','risk','recovery','approval_status'),
        'MFFU_RISK_EVIDENCE_FILE':('verified','account_controls_source','max_micro_contracts','required_evidence'),
    }.items():
        path=os.environ.get(key);item=dict(path=path,loaded=False)
        try:
            raw=Path(path).read_bytes() if path else b''
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError('object required')
            item.update(loaded=True,sha256=hashlib.sha256(raw).hexdigest(),
                        **{f:value.get(f) for f in fields})
        except (OSError,ValueError,TypeError):item['error']='FILE_MISSING_OR_INVALID'
        result[key]=item
    return result
