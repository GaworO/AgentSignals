"""Explicit closed-M1 admission policy for an ORIGINAL resting LIMIT.

Candle observations are never executable quotes. This policy does not certify
spread, passivity, intrabar validity or a bound on cancellation/stop slippage.
The independent thesis, exact AI, account, Guard and native protection gates
remain mandatory. No broker-writing operation is implemented here.
"""
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime

MODEL = 'RESTING_LIMIT_CLOSED_M1_V1'
SOURCE = 'AUTHENTICATED_TV_CLOSED_M1'
RISKS = ('unknown_spread', 'unknown_immediate_executability', 'intrabar_movement',
         'cancel_latency_and_late_fills', 'stop_slippage', 'broker_disconnects')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_policy(p):
    if (p.get('strategy') != 'AB_DIRECTIONAL' or p.get('approved') is not True
            or not p.get('validation_reference') or not p.get('residual_risk_acceptance_reference')
            or set(p.get('accepted_residual_risks', [])) != set(RISKS)):
        raise ValueError('resting_limit_policy_not_explicitly_accepted')
    if (type(p.get('max_quantity')) is not int or not 1 <= p['max_quantity'] <= 40
            or p.get('max_positions') != 1 or type(p.get('max_positions')) is not int
            or p.get('allow_scale_in') is not False or p.get('advanced_writes') is not False
            or p.get('original_limit_only') is not True or p.get('fixed_gross_rr') != 2
            or p.get('min_rr') != 2 or p.get('thesis_rule_version') != 'original_bos_bracket_ai_v1'):
        raise ValueError('resting_limit_quantity_cap_original_bracket_required')
    if not re.fullmatch(r'MNQ[HMUZ]20\d{2}', str(p.get('contract', ''))):
        raise ValueError('explicit_mnq_contract_required')
    if (type(p.get('contract_last_trade_ms')) is not int or p['contract_last_trade_ms'] <= 0
            or not p.get('contract_reference') or not p.get('account_binding')):
        raise ValueError('resting_limit_contract_account_reference_required')
    for key, cap in [('max_signal_age_ms', 120000), ('max_closed_m1_age_ms', 75000),
                     ('max_account_age_ms', 3000), ('max_thesis_age_ms', 3000),
                     ('max_pending_lifetime_sec', 600)]:
        if not finite(p.get(key)) or not 0 < p[key] <= cap:
            raise ValueError('resting_limit_parameter_invalid:' + key)
    # Quotes have no role in this explicitly selected policy. Reject ambiguous
    # mixed policy files instead of accidentally using indicative BBO.
    if any(p.get(k) is not None for k in ('quote_source', 'quote_verification')):
        raise ValueError('resting_limit_mixed_quote_policy')


def observation_check(order, observation, p, now):
    if type(order.get('quantity')) is not int or not 1 <= order['quantity'] <= p['max_quantity']:
        return 'resting_limit_quantity_cap'
    if (observation.get('source') != SOURCE or observation.get('order_id') != order['order_id']
            or observation.get('contract') != order['contract']
            or observation.get('closed') is not True or not observation.get('evidence_id')):
        return 'resting_limit_closed_candle_evidence_required'
    if observation.get('market_open') is not True:
        return 'market_closed_or_unknown'
    if observation.get('feed_authenticated') is not True:
        return 'resting_limit_authenticated_feed_required'
    opened, closed, received = (observation[k] for k in ('bar_open_ms', 'bar_close_ms', 'received_ms'))
    if (any(type(t) is not int for t in (opened, closed, received)) or opened % 60000
            or closed != opened + 60000 or not closed <= received <= now
            or not 0 <= now - closed <= p['max_closed_m1_age_ms']):
        return 'resting_limit_candle_stale_or_future'
    if closed != now // 60000 * 60000:
        return 'resting_limit_latest_closed_minute_missing'
    if not 0 <= now - observation['checked_ms'] <= p['max_thesis_age_ms']:
        return 'resting_limit_observation_stale'
    b = observation['ohlc']
    if (any(not finite(b.get(k)) for k in ('open', 'high', 'low', 'close'))
            or not 0 < b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']):
        return 'resting_limit_invalid_candle'
    # Structural validity of ALL post-setup bars is independently proved by
    # ThesisValidator. A candle close on the other side of the entry is not a
    # reason to worsen/reprice the LIMIT or pretend an executable price exists.
    return None


def assessment(order, observation, parameters):
    import execution_validation
    return dict(execution_model=MODEL, source=observation.get('source'), order_type='LIMIT',
        original_limit=order['entry_price'], stop=order['stop_price'], target=order['target_price'],
        bid=None, ask=None, spread_ticks=None, executable_price=None, immediate_marketability='UNKNOWN',
        executable_quote_verified=False, candle_observation_only=True,
        bar_close_ms=observation.get('bar_close_ms'), received_ms=observation.get('received_ms'),
        last_closed_candle=observation.get('ohlc'), residual_risks=list(RISKS),
        reward_risk_at_original_limit=execution_validation.reward_risk(order, parameters))


class CandleSource:
    def __init__(self, *, clock=time.time, market_open=None):
        self.clock, self.market_open = clock, market_open

    def __call__(self, order):
        import ab_v3_live
        import execution_validation
        p = execution_validation.policy()
        now = int(self.clock()*1000); end = now // 60000 * 60000
        if (p.get('execution_model') != MODEL or order['contract'] != p['contract']
                or os.environ.get('AB_V3_CONTRACT') != order['contract']
                or os.environ.get('EXEC_TICKER') != order['contract']):
            raise ValueError('resting_limit_contract_mismatch')
        if (not os.environ.get('AB_V3_FEED_TOKEN')
                or os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED') != '1'
                or os.environ.get('AB_V3_TV_MAPPING_VERIFIED') != '1'):
            raise ValueError('resting_limit_authenticated_contract_feed_required')
        if not callable(self.market_open) or self.market_open() is not True:
            raise ValueError('market_closed_or_unknown')
        with ab_v3_live.connect() as db:
            row = db.execute("SELECT ts_ms,bar_json,received_ms FROM bars WHERE tf='M1' AND contract=? AND ts_ms=?",
                             (order['contract'], end-60000)).fetchone()
        if row is None:raise ValueError('resting_limit_latest_closed_minute_missing')
        b = json.loads(row['bar_json'])
        stamp = datetime.fromisoformat(b['ts_event'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or int(stamp.timestamp()*1000) != row['ts_ms']:
            raise ValueError('resting_limit_candle_timestamp_mismatch')
        proof = dict(source=SOURCE, order_id=order['order_id'], contract=order['contract'],
            closed=True, feed_authenticated=True, market_open=True, bar_open_ms=row['ts_ms'],
            bar_close_ms=row['ts_ms']+60000, received_ms=row['received_ms'], checked_ms=now,
            ohlc={k:b[k] for k in ('open','high','low','close')},
            evidence_id=hashlib.sha256(json.dumps([order['contract'], dict(row)], sort_keys=True).encode()).hexdigest())
        reason = observation_check(order, proof, p, int(self.clock()*1000))
        if reason:raise ValueError(reason)
        return proof
