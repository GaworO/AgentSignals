"""Offline checks: no real AI/broker transport, no live configuration changes."""
import json
from unittest.mock import Mock

import pytest

import ab_ai_context as ai
import ab_v3_live as v3
import continuation_live
import directional_ai_review as gate
import execution_policy
import test_ab_routing as fixtures
import test_continuation_live as live_fixtures
import test_builder_crosstrade as native_fixtures


@pytest.fixture
def context(monkeypatch):
    f = fixtures.ContextTests()
    f.setUp()
    f.config['AB_AI_OWNER'] = 'AB_DIRECTIONAL'
    f.support = ai.ContextSupport(f.config, reader=f.source, transport=f.transport, clock=lambda: f.now)
    monkeypatch.setattr(ai, 'INSTANCE', f.support)
    monkeypatch.setenv('V3_AI_REVIEW_REQUIRED', 'true')
    try:
        yield f
    finally:
        f.doCleanups()


def test_directional_prompt_identity_cache_and_no_calls_on_entry(context):
    f = context
    assert f.support.process_one()
    payload = f.calls[0]
    assert json.loads(payload['input'][1]['content'])['owner'] == 'AB_DIRECTIONAL'
    assert 'valid A/B Directional setup' in payload['input'][0]['content']
    assert 'valid original A/B setup' not in payload['input'][0]['content']
    for side in ('LONG', 'SHORT'):
        reason, review = gate.check(side, side)
        assert reason is None and review['state'] == 'ALLOW'
    assert len(f.calls) == 1
    # Original A/B manager cannot borrow Directional's permission.
    assert ai.current(dict(dir='LONG'))['state'] == 'UNAVAILABLE'


def test_owner_change_cannot_reuse_previous_ab_review_or_reset_budget(context):
    f = context
    old = ai.ContextSupport(dict(f.config, AB_AI_OWNER='A/B'), reader=f.source,
                            transport=f.transport, clock=lambda: f.now)
    assert old.process_one()
    assert not f.support.process_one()
    assert gate.check('LONG', 'switched')[0] == 'v3_ai:unavailable'
    assert len(f.calls) == 1
    assert f.support.status == 'WAITING_FOR_INTERVAL'
    old_packet = old.packet(f.source(), f.now)
    new_packet = f.support.packet(f.source(), f.now)
    assert old_packet['packet_id'] != new_packet['packet_id']


@pytest.mark.parametrize('state', ['WAIT', 'VETO'])
def test_filter_uses_requested_side_and_keeps_explanation(context, state):
    f = context
    assert f.support.process_one()
    with f.support.connect() as db:
        row = db.execute('SELECT * FROM reviews').fetchone()
        decision = json.loads(row['decision'])
        decision['short_scenario']['permission'] = state
        db.execute('UPDATE reviews SET decision=?', (json.dumps(decision),))
    assert gate.check('LONG', 'long')[0] is None
    reason, review = gate.check('SHORT', 'short')
    assert reason == 'v3_ai:' + state.lower()
    assert review['scenario']['reason_pl'] == 'Potwierdzony kontekst'


@pytest.mark.parametrize('change', ['expired', 'stale_source', 'invalidated', 'disabled'])
def test_previous_allow_cannot_survive_invalid_context(context, change):
    f = context
    assert f.support.process_one()
    assert gate.check('LONG', 'initial')[0] is None
    if change == 'expired':
        f.now += 3601
    elif change == 'stale_source':
        f.now += 121
    elif change == 'disabled':
        f.support.enabled = False
    else:
        with f.support.connect() as db:
            db.execute("UPDATE reviews SET invalidated='[\"long_scenario\"]'")
    assert gate.check('LONG', 'dispatch')[0] is not None
    assert len(f.calls) == 1


def test_blocking_before_prepare_and_recheck_before_dispatch(context, monkeypatch):
    f = context
    monkeypatch.setenv('AB_V3_MODE', 'LIVE')
    monkeypatch.setattr(v3.builder_crosstrade, 'entry_blocker',
                        Mock(side_effect=AssertionError('broker read before failed AI gate')))
    order = dict(strategy='AB_DIRECTIONAL', order_id='offline', direction='SHORT')
    assert v3.entry_blocker(order) == 'v3_ai:unavailable'
    assert order['_directional_ai_review']['state'] == 'UNAVAILABLE'
    f.support.process_one()
    assert gate.check('SHORT', 'prepared')[0] is None
    f.now += 121
    signal = dict(_v3_directional=True, _continuation_order_id='offline', dir='SHORT')
    assert v3.prepared_entry_blocker(signal) == 'v3_ai:unavailable'
    assert signal['_directional_ai_review']['state'] == 'UNAVAILABLE'


def test_off_bad_flag_and_bad_direction_never_call_ai(context, monkeypatch):
    monkeypatch.setattr(ai, 'annotate', Mock(side_effect=AssertionError('unexpected cache read')))
    monkeypatch.setenv('V3_AI_REVIEW_REQUIRED', 'false')
    assert gate.check('LONG', 'a') == (None, None)
    monkeypatch.setenv('V3_AI_REVIEW_REQUIRED', 'typo')
    assert gate.check('LONG', 'b')[0] == 'v3_ai:invalid_required_flag'
    monkeypatch.setenv('V3_AI_REVIEW_REQUIRED', 'true')
    assert gate.check('UNKNOWN', 'c')[0] == 'v3_ai:invalid_direction'


def test_dashboard_marks_review_owner_and_does_not_disclose_request(context, monkeypatch):
    from flask import Flask
    f = context
    f.support.process_one()
    app = Flask(__name__)
    ai.register(app)
    data = app.test_client().get('/ab/ai-context').get_json()
    assert data['owner'] == 'AB_DIRECTIONAL'
    assert data['mode'] == 'DIRECTIONAL_ENTRY_FILTER'
    assert data['reviews'][0]['owner'] == 'AB_DIRECTIONAL'
    assert 'request' not in data['reviews'][0]
    assert len(f.calls) == 1


def test_shadow_audits_veto_without_calling_dispatcher(context, monkeypatch):
    f = live_fixtures.ContinuationLiveTests()
    f.setUp()
    monkeypatch.setenv('EXEC_STRATEGY_POLICY', 'SHADOW_ALL')
    monkeypatch.setattr(v3, 'record_dispatch', Mock())
    dispatcher = Mock(side_effect=AssertionError('shadow must not dispatch'))
    try:
        f._bar(4_000_000)
        continuation_live.configure(dispatcher)
        continuation_live.drain()
        f._order(4_060_000, suffix='ai', expiry=4_600_000, strategy='AB_DIRECTIONAL')
        f._bar(4_060_000)
        result = continuation_live.drain()['results'][0]
        assert result['state'] == 'SHADOW'
        assert result['ai_would_block'] == 'v3_ai:unavailable'
        assert result['ai_review']['owner'] == 'AB_DIRECTIONAL'
        dispatcher.assert_not_called()
        assert context.calls == []
    finally:
        f.tearDown()


def test_directional_policy_keeps_original_ab_in_shadow(monkeypatch):
    monkeypatch.setenv('EXEC_STRATEGY_POLICY', 'V3_ONLY')
    assert execution_policy.shadow_family('A/B')
    assert not execution_policy.shadow_family('AB_DIRECTIONAL')


def test_invalid_owner_rejected_before_paid_call(context):
    with pytest.raises(ValueError, match='invalid_context_owner'):
        ai.ContextSupport(dict(context.config, AB_AI_OWNER='typo'))


@pytest.mark.parametrize('allowed', [False, True])
def test_native_transport_rechecks_after_account_refresh(context, monkeypatch, allowed):
    """A valid upstream check cannot authorize a stale context after broker GETs."""
    f = native_fixtures.NativeTests()
    f.setUp()
    try:
        f.prepare()
        permission = dict(owner='AB_DIRECTIONAL', state='ALLOW' if allowed else 'INVALIDATED',
                          review_id='final-review')
        monkeypatch.setattr(ai, 'annotate', Mock(return_value=permission))
        if allowed:
            assert f.t.submit(f.payload(), 'order-one')['sent']
            with f.api.connect() as db:
                command = json.loads(db.execute('SELECT command_json FROM native_orders').fetchone()[0])
            assert command['order']['ai_review_at_native_dispatch'] == permission
            assert len(f.posts) == 1  # Injected in-memory transport only.
        else:
            with pytest.raises(native_fixtures.native.TransportError, match='v3_ai:invalidated'):
                f.t.submit(f.payload(), 'order-one')
            assert f.posts == []
            with f.api.connect() as db:
                assert db.execute('SELECT COUNT(*) FROM native_orders').fetchone()[0] == 0
        assert ai.annotate.call_args.args[1] == 'order-one:native'
        assert context.calls == []
    finally:
        f.doCleanups()
