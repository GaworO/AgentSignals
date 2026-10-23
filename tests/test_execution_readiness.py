import copy
import json
import os

import pytest

import execution_safety as safety
import execution_validation as validation
import builder_crosstrade as native
import test_builder_crosstrade as fixture


def evidence():
    now = 1000000
    order = dict(order_id='one', strategy='AB_DIRECTIONAL', direction='LONG',
                 contract='MNQZ2026', activation_ms=now-1000, expiry_ms=now+60000,
                 entry_price=100., stop_price=90., target_price=120.)
    # Synthetic protocol parameters, NOT research-approved production limits.
    limits = dict(approved=True, validation_reference='SYNTHETIC_TEST_ONLY',
                  strategy='AB_DIRECTIONAL', contract='MNQZ2026', quote_source='crosstrade_nt8',
                  contract_last_trade_ms=9999999999999,contract_reference='SYNTHETIC_TEST_ONLY',
                  max_signal_age_ms=30000, max_quote_age_ms=1000, entry_zone_points=.5,
                  max_spread_ticks=2, max_slippage_ticks=1, max_volatility_points=10,
                  volatility_definition='fixture_range', min_rr=2,
                  max_account_age_ms=1000,max_thesis_age_ms=1000)
    limits['quote_verification']=dict(verified=True,source='crosstrade_nt8',contract='MNQZ2026',
        reference='SYNTHETIC_TEST_ONLY',verified_at_ms=1,expires_at_ms=9999999999999,timestamp_semantics='bid_ask_event_time',venue_bbo=True)
    limits['costs']=dict(contract='MNQZ2026',validation_reference='ZERO_COST_SYNTHETIC_ONLY',
                         round_trip_usd=0,stop_slippage_ticks=0)
    quote = dict(contract='MNQZ2026', source='crosstrade_nt8', market_open=True,
                 execution_eligible=True,timestamp_semantics_verified=True,
                 as_of_ms=now, request_latency_ms=10, bid=99.75, ask=100., volatility_points=4,
                 volatility_definition='fixture_range')
    thesis = dict(order_id='one', valid=True, checked_ms=now, evidence_id='closed-m1-fixture')
    risk = dict(order_id='one', allowed=True, checked_ms=now, account_binding='simulator')
    review = dict(order_id='one', state='ALLOW', review_id='fixture-review', available_at=999,
                  expires_at=1060, setup=copy.deepcopy(order))
    return [order, quote, thesis, risk, review, limits, now]


def test_master_default_off_and_kill_preserves_protective_writes(monkeypatch):
    monkeypatch.delenv('LIVE_EXECUTION_ENABLED', raising=False)
    assert safety.entry_blocker() == 'live_execution_disabled'
    monkeypatch.setenv('LIVE_EXECUTION_ENABLED', 'TRUE')
    assert not safety.writes_enabled()
    monkeypatch.setenv('LIVE_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('EXEC_KILL_SWITCH', 'true')
    assert safety.entry_blocker() == 'execution_kill_switch'
    assert safety.writes_enabled()


@pytest.mark.parametrize('index,key,value,reason', [
    (0, 'expiry_ms', 1000000, 'signal_expired'),
    (0, 'activation_ms', 900000, 'signal_too_old'),
    (1, 'as_of_ms', 998000, 'quote_stale_or_future'),
    (1, 'contract', 'MNQU2026', 'contract_mismatch'),
    (1, 'market_open', False, 'market_closed_or_unknown'),
    (1, 'source', 'candle', 'quote_source_unverified'),
    (1, 'ask', 101, 'outside_validated_entry_zone'),
    (1, 'bid', 99, 'spread_limit'),
    (1, 'ask', 100.5, 'spread_limit'),
    (1, 'volatility_points', 11, 'volatility_limit_or_definition'),
    (1, 'volatility_definition', 'another', 'volatility_limit_or_definition'),
    (1, 'ask', 100.1, 'off_tick_price'),
    (2, 'valid', False, 'directional_thesis_not_revalidated'),
    (3, 'allowed', False, 'account_risk_not_revalidated'),
    (4, 'state', 'WAIT', 'setup_review_missing_or_expired'),
    (4, 'expires_at', 1000, 'setup_review_missing_or_expired'),
])
def test_invalid_execution_evidence(index, key, value, reason):
    args = evidence()
    args[index][key] = value
    assert validation.evaluate(*args) == reason


def test_allow_is_insufficient_changed_bracket_or_missing_evidence():
    args = evidence()
    assert validation.evaluate(*args) is None
    args[4]['setup']['target_price'] = 130
    assert validation.evaluate(*args) == 'setup_review_geometry_mismatch'
    args[1].pop('bid')
    assert validation.evaluate(*args) == 'revalidation_evidence_incomplete'


def test_adverse_move_reduces_rr_even_inside_zone():
    args = evidence()
    args[1].update(bid=100, ask=100.25)
    assert validation.evaluate(*args) == 'original_limit_exceeded'
    args[1].update(bid=100.25, ask=100.5)
    assert validation.evaluate(*args) == 'slippage_limit'


def test_production_without_approved_parameters_and_sources_is_blocked(monkeypatch, tmp_path):
    monkeypatch.delenv('DIRECTIONAL_EXECUTION_POLICY_FILE', raising=False)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(validation, '_SOURCES', {})
    args = evidence()
    assert validation.check(args[0], args[4]) == 'validated_execution_policy_missing'
    assert not validation.status()['ready']
    path = tmp_path/'policy.json'
    path.write_text(json.dumps(args[5]))
    monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE', str(path))
    assert validation.check(args[0], args[4]) == 'risk_validator_unavailable'
    args[5]['min_rr'] = 1.5
    path.write_text(json.dumps(args[5]))
    assert validation.check(args[0], args[4]) == 'directional_minimum_2r_required'


def activation():
    return dict(order=dict(id=13, ocoId=12, ordStatus='Working'),
                version=dict(id=20), commands=[
                    dict(id=13, commandType='New', commandStatus='ExecutionStopped'),
                    dict(id=20, orderId=13, commandType='Modify', commandStatus='AtExecution', isAutomated=True)],
                reports=[dict(id=14, commandId=13, commandStatus='OnHold'),
                         dict(id=21, commandId=20, commandStatus='AtExecution')])


def test_native_oso_activation_shape_from_observed_protocol():
    assert native.stable_version(activation())
    for change in ('cancel', 'foreign', 'manual', 'rejected', 'pending', 'closed', 'second_modify'):
        life = activation()
        if change == 'cancel':
            life['commands'].append(dict(commandType='Cancel'))
        elif change == 'foreign':
            life['commands'][1]['orderId'] = 999
        elif change == 'manual':
            life['commands'][1]['isAutomated'] = False
        elif change == 'rejected':
            life['reports'][-1]['rejectReason'] = 'Risk'
        elif change == 'pending':
            life['reports'][-1]['commandStatus'] = 'Pending'
        elif change == 'closed':
            life['order']['ordStatus'] = 'Filled'
        else:
            life['commands'].append(dict(id=22, commandType='Modify'))
        assert not native.stable_version(life), change


@pytest.fixture
def simulator(monkeypatch):
    instance = fixture.NativeTests()
    instance.setUp()
    instance.order['strategy'] = 'AB_DIRECTIONAL'
    instance.prepare()
    try:
        yield instance
    finally:
        instance.doCleanups()


def test_native_rechecks_gate_after_account_refresh_and_no_post_on_failure(simulator):
    f = simulator
    f.final_gate.return_value = 'quote_stale_or_future'
    result = f.t.submit(f.payload(), f.order['order_id'])
    assert not result['sent'] and not result['submission_unknown']
    assert f.posts == []
    assert f.t.row(f.order['order_id'])['state'] == 'NO_FILL'


def test_shadow_ab_cannot_reach_native_adapter(simulator):
    f = simulator
    f.order['strategy'] = 'A/B'
    f.prepare()
    with pytest.raises(native.TransportError, match='SHADOW_STRATEGY'):
        f.submit()
    assert f.posts == []


def test_simulated_gate_native_ack_fill_restart_and_stop_with_costs(simulator, monkeypatch, tmp_path):
    f = simulator
    args = evidence()
    args[0] = dict(f.order)
    args[-1] = int(f.now*1000)
    args[1].update(bid=29999.75, ask=30000, as_of_ms=args[-1])
    for item in args[2:4]:
        item.update(order_id=f.order['order_id'], checked_ms=args[-1])
    args[4].update(order_id=f.order['order_id'], setup=dict(f.order),
                   available_at=f.now-1, expires_at=f.now+60)
    f.final_gate.side_effect = lambda order, review: validation.evaluate(
        order, args[1], args[2], args[3], args[4], args[5], args[-1])
    result = f.submit()
    assert result['sent'] and f.local()['position_json'] is None
    f.accept(f.order, dict(state='SENT', quantity=2, broker=result))
    f.open()
    assert f.local()['state'] == 'BROKER_CONFIRMED_OPEN'
    restarted = native.Transport(f.api, f.sync, request=f.request, clock=lambda:f.now)
    snapshot, net = f.complete(winning=False)
    restarted.reconcile_one(restarted.row(f.order['order_id']), snapshot)
    assert f.local()['state'] == 'CLOSED'
    assert net == -42.4
    assert len(f.posts) == 1
