"""Original strategy → native writer contract. All broker/quote IO is mocked.

No contents of this module constitute provider-paper evidence.
"""
import copy
import json
import pytest
from unittest.mock import patch
import test_builder_crosstrade as protocol
import directional_management_broker as bridge
import directional_management_status as visibility
import execution_validation

@pytest.fixture
def h():
    case=protocol.NativeTests();case.setUp()
    try:
        case.submit();case.open();case.exit_payload=case.original_exit_fixture()
        yield case
    finally:case.doCleanups()


def test_original_full_exit_one_writer_and_restart_dedup(h):
    assert bridge.original_exit(h.t,h.exit_payload)['success']
    assert len(h.posts)==2 and h.posts[-1][0].endswith('/positions/close')
    assert h.posts[-1][1]=={'instrument':'MNQZ6'}
    h.life[14]=dict(order=dict(id=14,accountId=2214,contractId=100,action='Sell',ordStatus='Working'),
        version=dict(orderId=14,orderQty=2,orderType='Market'),commands=[],reports=[])
    restarted=protocol.native.Transport(h.api,h.sync,request=h.request,clock=lambda:h.now)
    assert bridge.original_exit(restarted,h.exit_payload)['already_reserved']
    assert len(h.posts)==2


@pytest.mark.parametrize('change',[
    lambda p:p.pop('broker_workflow_verification'),
    lambda p:p['broker_workflow_verification'].update(simulator_only=True),
    lambda p:p['broker_workflow_verification'].update(account_binding='another'),
    lambda p:p['broker_workflow_verification'].update(contract='NQZ2026'),
    lambda p:p['broker_workflow_verification']['management'].update(terminal_children=False),
    lambda p:p['broker_workflow_verification']['management'].update(expires_at_ms=1),
    lambda p:p['broker_workflow_verification']['management'].update(verified_at_ms=10**20),
])
def test_paper_proof_is_required_before_quote_or_close(h,change):
    change(h.manager_policy)
    with pytest.raises(ValueError,match='PAPER_PROOF'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


@pytest.mark.parametrize('change',[
    lambda q:q.update(source='collective',execution_eligible=False),
    lambda q:q.update(source='drafted'),
    lambda q:q.update(timestamp_semantics_verified=False),
    lambda q:q.update(as_of_ms=None),
    lambda q:q.update(as_of_ms=1),
    lambda q:q.update(ask=30002.),
    lambda q:q.update(market_open=False),
])
def test_unknown_stale_or_unsuitable_quote_never_closes(h,change):
    change(h.exit_quote)
    with pytest.raises(ValueError,match='EXECUTABLE_QUOTE'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


@pytest.mark.parametrize('change',[
    lambda d:d.update(decision='HOLD'),
    lambda d:d.update(m3_confirmed=False),
    lambda d:d.update(boundary={'classification':'RECOVERY'}),
    lambda d:d.update(reason='B13'),
    lambda d:d.update(known_ms=1),
])
def test_original_rule_veto_cannot_be_overridden_by_intent(h,change):
    with h.api.connect() as c:
        d=json.loads(c.execute('SELECT payload_json FROM manager_states').fetchone()[0]);change(d)
        c.execute('UPDATE manager_states SET payload_json=?',(json.dumps(d),))
    with pytest.raises(ValueError,match='M3_CONFIRMATION'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_tampered_payload_or_shadow_strategy_cannot_close(h):
    payload=copy.deepcopy(h.exit_payload);payload['quantity']=1
    with pytest.raises(ValueError,match='IDENTITY'):bridge.original_exit(h.t,payload)
    with h.api.connect() as c:
        o=copy.deepcopy(h.order);o['strategy']='AB'
        c.execute('UPDATE orders SET order_json=?',(json.dumps(o),))
    with pytest.raises(ValueError,match='IDENTITY'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_expiry_or_interlock_revocation_during_quote_request(h):
    def delayed(*a,**k):
        h.now+=11
        return copy.deepcopy(h.exit_quote)
    with patch('directional_market_data.QuoteClient.fetch',side_effect=delayed):
        with pytest.raises(ValueError,match='SIGNAL_EXPIRED'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_slow_broker_read_rechecks_quote_and_prevents_post(h):
    poll=h.sync.poll
    def slow(*a,**k):
        h.now+=4
        return poll(*a,**k)
    with patch.object(h.sync,'poll',side_effect=slow):
        with pytest.raises(ValueError,match='EXECUTABLE_QUOTE'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_policy_revocation_after_broker_read_prevents_post(h):
    policy=copy.deepcopy(h.manager_policy);poll=h.sync.poll
    def revoke(*a,**k):
        execution_validation.policy.return_value={}
        return poll(*a,**k)
    with patch.object(h.sync,'poll',side_effect=revoke):
        with pytest.raises(ValueError,match='POLICY_CHANGED'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_timeout_close_has_durable_unknown_and_is_never_resent(h):
    h.timeout=True
    assert not bridge.original_exit(h.t,h.exit_payload)['success']
    h.timeout=False
    restarted=protocol.native.Transport(h.api,h.sync,request=h.request,clock=lambda:h.now)
    assert bridge.original_exit(restarted,h.exit_payload)['already_reserved']
    assert len(h.posts)==2


def test_guard_full_close_remains_independent_of_discretionary_approval(h):
    h.manager_policy.clear();h.exit_quote.clear()
    assert h.t.action('order-one','close','guard-fixture')['success']
    assert len(h.posts)==2


def test_observer_saves_existing_policy_without_dispatching(h):
    bridge.observe_native(h.t,'order-one',h.sync.snapshot())
    result=visibility.status(h.t,int(h.now*1000))
    assert result['positions'][0]['original_policy_decision']['reason']=='F2'
    assert len(h.posts)==1

@pytest.mark.parametrize('selected,expected',[(False,True),(True,False)])
def test_only_original_model0_exit2r_decision_can_request_full_exit(h,selected,expected):
    now=int(h.now*1000)
    h.exit_payload['extras']['v3PrimaryReason']='RUNNER_2R_EXIT'
    h.order['runner_enabled']=True
    with h.api.connect() as c:
        c.execute('UPDATE orders SET order_json=?',(json.dumps(h.order),))
        c.execute('UPDATE actions SET payload_json=?',(json.dumps(h.exit_payload),))
        c.execute("UPDATE exit_trades SET primary_manager='RUNNER',primary_reason='RUNNER_2R_EXIT'")
        d=dict(touch_ms=now,selected=selected,decision_kind='HOLD_3R' if selected else 'EXIT_2R')
        c.execute('INSERT INTO runner_decisions VALUES(?,?,?,?,?,?)',('order-one',now,now-60000,int(selected),json.dumps(d),now))
    if expected:
        assert bridge.original_exit(h.t,h.exit_payload)['success']
        assert len(h.posts)==2
    else:
        with pytest.raises(ValueError,match='RUNNER_CONFIRMATION'):bridge.original_exit(h.t,h.exit_payload)
        assert len(h.posts)==1


def test_exit_permission_revoked_during_broker_poll_blocks(h):
    poll=h.sync.poll
    def revoke(*a,**k):
        h.api.exit_blockers=lambda:['real_execution_not_enabled']
        return poll(*a,**k)
    with patch.object(h.sync,'poll',side_effect=revoke):
        with pytest.raises(ValueError,match='EXECUTION_INTERLOCK'):bridge.original_exit(h.t,h.exit_payload)
    assert len(h.posts)==1


def test_known_presend_veto_does_not_lock_out_guard(h):
    count=0
    def veto():
        nonlocal count
        count+=1
        if count==2:raise ValueError('fixture permission revoked after reservation')
    result=h.t.action('order-one','close','vetoed',before_send=veto)
    assert result=={'success':False,'not_sent':True} and len(h.posts)==1
    assert not h.t.action('order-one','close','vetoed')['success']
    assert h.t.action('order-one','close','guard-after-veto')['success']
    assert len(h.posts)==2


def test_runner_entry_requires_proven_full_close_before_network(h):
    order=dict(h.order,runner_enabled=True)
    assert h.t.runner_entry_blocker(order) is None
    h.manager_policy['broker_workflow_verification']['management']['full_exit']=False
    assert h.t.runner_entry_blocker(order)=='NATIVE_RUNNER_FULL_EXIT_PAPER_PROOF_REQUIRED'
    assert h.t.runner_entry_blocker(dict(order,runner_enabled=False)) is None
    assert len(h.posts)==1


def test_master_write_permission_revoked_before_io_is_known_not_sent(h):
    import os
    calls=0
    def revoke():
        nonlocal calls
        calls+=1
        if calls==2:os.environ['LIVE_EXECUTION_ENABLED']='false'
    assert h.t.action('order-one','close','revoked-master',before_send=revoke)['not_sent']
    assert len(h.posts)==1
