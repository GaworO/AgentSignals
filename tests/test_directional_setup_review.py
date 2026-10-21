"""Setup-triggered review tests use only fake AI responses and temporary SQLite."""
import copy
import json
import threading
from unittest.mock import Mock

import pytest

import ab_ai_context as ai
import ab_directional_entry_rules as rules
import directional_ai_review as gate
import test_ab_routing as fixtures
import test_continuation_live as live_fixtures
import continuation_live


@pytest.fixture
def service(monkeypatch):
    f = fixtures.ContextTests()
    f.setUp()
    f.config.update(AB_AI_OWNER='AB_DIRECTIONAL', AB_AI_TRIGGER='SETUP',
                    AB_V3_CONTRACT='MNQZ2026', AB_AI_SETUP_MAX_AGE_SECONDS='120')
    f.support = ai.ContextSupport(f.config, reader=f.source, transport=f.transport, clock=lambda: f.now)
    monkeypatch.setattr(ai, 'INSTANCE', f.support)
    monkeypatch.setenv('AB_AI_TRIGGER', 'SETUP')
    try:
        yield f
    finally:
        f.doCleanups()


def order(f, oid='one'):
    at = int(f.now)//60*60000
    evidence = rules.evaluate('PDH', 'LONG', dict(open=100, high=104, low=96, close=104),
                              at-60000, at)
    return dict(strategy='AB_DIRECTIONAL', order_id=oid, candidate_id='candidate-'+oid,
                direction='LONG', entry_price=100, stop_price=90, target_price=120,
                activation_ms=at, expiry_ms=at+600000, candidate_payload_json='{"brk":2}',
                payload_json=json.dumps(dict(entry_rules=evidence, setup_category='PDH',
                    v3_snapshot=dict(source_level=99, fvg_edge=99), net_pnl=99999, future_outcome='win')))


def test_no_setup_no_source_fetch_or_paid_call(service):
    f = service
    f.support.reader = Mock(side_effect=AssertionError('unexpected source read'))
    assert not f.support.process_one()
    assert f.support.status == 'WAITING_FOR_SETUP'
    assert f.calls == []


def test_setup_queue_deduplicates_and_packet_contains_causal_geometry(service):
    f = service
    o = order(f)
    assert f.support.queue_setup(o)['state'] == 'queued'
    assert f.support.queue_setup(o)['state'] == 'queued'
    assert f.calls == []
    assert f.support.process_one()
    assert not f.support.process_one()
    assert len(f.calls) == 1
    p = json.loads(f.calls[0]['input'][1]['content'])
    assert p['setup']['order_id'] == 'one'
    assert p['setup']['entry_price'] == 100
    assert p['setup']['alternative_3r'] == 130
    assert p['setup']['risk_points'] == 10
    assert p['setup']['source_level'] == 99
    assert 'future_outcome' not in json.dumps(p)
    assert 'net_pnl' not in json.dumps(p)
    assert 'No setup or filled position is supplied.' not in f.calls[0]['input'][0]['content']
    assert 'specific setup' in f.calls[0]['input'][0]['content']
    assert f.support.queue_setup(o)['state'] == 'validated'


def test_changed_geometry_cannot_borrow_or_overwrite_previous_review(service):
    f = service
    o = order(f)
    f.support.queue_setup(o)
    changed = dict(o, target_price=130)
    assert f.support.queue_setup(changed)['state'] == 'SETUP_CHANGED'
    assert f.support.process_one()
    assert json.loads(f.calls[0]['input'][1]['content'])['setup']['target_price'] == 120
    with f.support.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM setup_requests').fetchone()[0] == 1


def test_different_setups_get_different_reviews_without_hourly_wait(service):
    f = service
    for oid in ('first', 'second'):
        f.support.queue_setup(order(f, oid))
        assert f.support.process_one()
    packets = [json.loads(x['input'][1]['content']) for x in f.calls]
    assert [p['setup']['order_id'] for p in packets] == ['first', 'second']
    assert packets[0]['packet_id'] != packets[1]['packet_id']
    assert f.support.annotate(dict(dir='LONG'), 'other', owner='AB_DIRECTIONAL')['state'] == 'UNAVAILABLE'


def test_switch_back_to_periodic_does_not_reuse_setup_permission(service):
    f = service
    f.support.queue_setup(order(f))
    f.support.process_one()
    periodic = ai.ContextSupport(dict(f.config, AB_AI_TRIGGER='PERIODIC'),
                                reader=f.source, transport=f.transport, clock=lambda: f.now)
    periodic.process_one()  # refresh data, interval prevents another API call
    assert periodic.annotate(dict(dir='LONG'), 'other', owner='AB_DIRECTIONAL')['state'] == 'UNAVAILABLE'
    assert len(f.calls) == 1


def test_twelve_attempts_shared_with_existing_usage_and_restart(service):
    f = service
    f.used = 10
    for oid in ('one', 'two', 'three'):
        f.support.queue_setup(order(f, oid))
        f.support.process_one()
    assert len(f.calls) == 2
    assert f.support.status == 'DAILY_LIMIT_REACHED'
    f.support = ai.ContextSupport(f.config, reader=f.source, transport=f.transport, clock=lambda: f.now)
    f.support.queue_setup(order(f, 'four'))
    assert not f.support.process_one()
    assert len(f.calls) == 2


def test_expired_queued_setup_does_not_consume_budget(service):
    f = service
    f.support.queue_setup(order(f))
    f.now += 121
    assert not f.support.process_one()
    assert f.calls == []
    with f.support.connect() as db:
        assert db.execute('SELECT state FROM setup_requests').fetchone()[0] == 'expired'


def test_slow_api_answer_is_expired_and_still_counts(service):
    f = service
    def slow(payload):
        response = f.transport(payload)
        f.now += 121
        return response
    f.support.transport = slow
    f.support.queue_setup(order(f))
    assert f.support.process_one()
    assert f.support.status == 'expired'
    with f.support.connect() as db:
        assert db.execute('SELECT state FROM setup_requests').fetchone()[0] == 'expired'
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 1


def test_unknown_response_is_not_retried_after_restart(service):
    f = service
    f.support.queue_setup(order(f))
    def timeout(payload):
        f.calls.append(payload)
        raise TimeoutError()
    f.support.transport = timeout
    assert f.support.process_one()
    assert f.support.status == 'unknown'
    fresh = ai.ContextSupport(f.config, reader=f.source, transport=f.transport, clock=lambda: f.now)
    fresh.queue_setup(order(f, 'another'))
    assert not fresh.process_one()
    assert fresh.status == 'PENDING_OR_UNKNOWN_NO_RETRY'
    assert len(f.calls) == 1


def test_concurrent_workers_reserve_one_attempt(service):
    f = service
    f.support.queue_setup(order(f))
    barrier = threading.Barrier(2)
    def read():
        barrier.wait(timeout=5)
        return f.source()
    workers = [ai.ContextSupport(f.config, reader=read, transport=f.transport, clock=lambda: f.now)
               for _ in range(2)]
    results = []
    threads = [threading.Thread(target=lambda w=w: results.append(w.process_one())) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)
    assert sorted(results) == [False, True]
    assert len(f.calls) == 1


@pytest.mark.parametrize('change', ['future', 'outside_session', 'old_rules', 'invalid_geometry'])
def test_ineligible_setup_never_calls_ai(service, change):
    f = service
    o = order(f)
    if change == 'future':
        o['activation_ms'] += 60000
        o['payload_json'] = '{}'  # also lacks the new causal evidence
    elif change == 'outside_session':
        f.now += 3600  # 11:00 NY
    elif change == 'old_rules':
        o['payload_json'] = '{}'
    else:
        o['stop_price'] = 110
    if change == 'invalid_geometry':
        with pytest.raises(ValueError, match='invalid_setup_geometry'):
            f.support.queue_setup(o)
    else:
        assert f.support.queue_setup(o)['state'] != 'queued'
    assert not f.support.process_one()
    assert f.calls == []


def test_setup_mode_cannot_bypass_missing_review_with_optional_gate_off(service, monkeypatch):
    monkeypatch.setenv('V3_AI_REVIEW_REQUIRED', 'false')
    assert gate.check('LONG', 'one')[0] == 'v3_ai:unavailable'
    assert gate.check('SHORT', 'two')[1]['executable'] is False
    assert service.calls == []


def test_status_exposes_exact_setup_latency_and_expiry(service):
    from flask import Flask
    f = service
    def latency(payload):
        response = f.transport(payload)
        f.now += 54
        return response
    f.support.transport = latency
    f.support.queue_setup(order(f))
    f.support.process_one()
    app = Flask(__name__)
    ai.register(app)
    data = app.test_client().get('/ab/ai-context').get_json()
    assert data['mode'] == 'DIRECTIONAL_SETUP_REVIEW_SHADOW'
    assert data['trigger'] == 'SETUP'
    assert data['setup_requests'][0]['order_id'] == 'one'
    review = data['reviews'][0]
    assert review['setup']['entry_price'] == 100
    assert review['latency_seconds'] == 54
    assert review['expires_at'] == data['setup_requests'][0]['expires']
    assert data['executable'] is False
    assert 'request' not in review


def test_exact_setup_allow_is_bound_to_direction_and_expires(service):
    f = service
    f.support.queue_setup(order(f))
    assert gate.check('LONG', 'one:entry')[0] == 'v3_ai:pending'
    f.support.process_one()
    reason, review = gate.check('LONG', 'one:native')
    assert reason is None
    assert review['order_id'] == 'one' and review['setup']['entry_price'] == 100
    assert review['executable'] is False  # final market/risk gate still required
    assert gate.check('SHORT', 'one:native')[0] == 'v3_ai:setup_mismatch'
    assert gate.check('LONG', 'another:native')[0] == 'v3_ai:unavailable'
    f.now += 121
    assert gate.check('LONG', 'one:native')[0] == 'v3_ai:expired'


def test_pending_dispatch_resumes_once_and_never_retries_unknown(service, monkeypatch, tmp_path):
    f = service
    monkeypatch.setattr(continuation_live, 'DB_PATH', tmp_path/'live.sqlite3')
    monkeypatch.setenv('EXEC_STRATEGY_POLICY', 'V3_ONLY')
    monkeypatch.setenv('AB_DIRECTIONAL_LIVE_LONG', '1')
    monkeypatch.setattr(continuation_live.ab_v3_live, 'record_dispatch', Mock())
    row = order(f)
    row['expiry_ms'] = 2100000000000
    send = Mock(return_value=dict(state='SUBMISSION_UNKNOWN', reason='timeout'))
    continuation_live.configure(send)
    assert continuation_live._claim(row, 'fixture')
    continuation_live._finish(row['order_id'], dict(state='WAITING_AI'), 'fixture')
    check = Mock(return_value=('v3_ai:pending', dict(state='PENDING')))
    monkeypatch.setattr(gate, 'check', check)
    continuation_live.resume_reviews()
    send.assert_not_called()
    check.return_value = (None, dict(state='ALLOW'))
    continuation_live.resume_reviews()
    continuation_live.resume_reviews()
    assert send.call_count == 1
    assert continuation_live.rows()[0]['state'] == 'SUBMISSION_UNKNOWN'


@pytest.mark.parametrize('break_number, expected', [(2, 1), (1, 0)])
def test_detector_queues_only_eligible_forward_setup_without_dispatch(service, monkeypatch, break_number, expected):
    f = service
    adapter = live_fixtures.ContinuationLiveTests()
    adapter.setUp()
    monkeypatch.setenv('EXEC_STRATEGY_POLICY', 'SHADOW_ALL')
    monkeypatch.setattr(continuation_live.ab_v3_live, 'record_dispatch', Mock())
    send = Mock(side_effect=AssertionError('no broker dispatch allowed'))
    try:
        at = int(f.now)*1000
        adapter._bar(at-60000)
        continuation_live.configure(send)
        continuation_live.drain()
        adapter._order(at, strategy='AB_DIRECTIONAL')
        with live_fixtures.shadow._connect() as db:
            db.execute('UPDATE continuation_candidates SET payload_json=?', (json.dumps(dict(brk=break_number)),))
        adapter._bar(at)
        result = continuation_live.drain()
        assert result['results'][0]['state'] == 'SHADOW'
        with f.support.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM setup_requests').fetchone()[0] == expected
        assert f.calls == []
        send.assert_not_called()
        assert continuation_live.drain()['processed'] == 0
    finally:
        adapter.tearDown()
