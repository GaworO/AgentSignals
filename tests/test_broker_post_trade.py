import json
from unittest.mock import patch

import pytest
import broker_post_trade
import builder_crosstrade as native
import test_builder_crosstrade as protocol


@pytest.fixture
def closed():
    f=protocol.NativeTests();f.setUp()
    try:
        f.submit();f.open();snapshot,_=f.complete()
        f.t.reconcile_one(f.t.row('order-one'),snapshot)
        f.t.reconcile_costs(snapshot)
        assert f.t.row('order-one')['state']=='CLOSED'
        with patch('directional_account_reconciliation.state',return_value=dict(verified=True,missing=[])):
            yield f
    finally:f.doCleanups()


def test_closed_round_trip_settled_without_extra_provider_reads_or_orders(closed):
    f=closed
    with patch.object(f.t,'request',side_effect=AssertionError('No provider IO')),patch.object(f.sync,'poll',side_effect=AssertionError('No extra account poll')):
        assert f.t.post_trade.check()
        state=f.t.post_trade.state()
        assert state['state']=='SETTLED' and state['level']=='ok'
        assert not state['authorizes_entries'] and state['reasons']==[]
        assert not f.t.post_trade.check()
    with f.api.connect() as c:
        assert c.execute("SELECT count(*) FROM native_audit WHERE kind='POST_TRADE_CHECK'").fetchone()[0]==1


@pytest.mark.parametrize('problem,expected',[
    ('action','native_actions_unresolved'),('costs','native_order_or_costs_unsettled'),
    ('fees','closed_trade_fees_unreconciled'),('local','local_closed_state_mismatch'),
    ('quantity','closed_trade_fill_quantities_differ'),('queue','recovery_request_pending'),
    ('alert','operator_alert_unresolved'),('phase','recovery_phase_pending'),
    ('account','account:fee_adjustment_pending'),('stale','broker_account_not_current')])
def test_block_is_visible_then_escalates_without_clearing_evidence(closed,problem,expected):
    f=closed;b=f.t.binding
    with f.api.connect() as c:
        if problem=='action':c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',(b,'test-close','order-one','close','UNKNOWN','{}','{}',int(f.now*1000)))
        if problem=='costs':c.execute("UPDATE native_orders SET state='CLOSED_PENDING_COSTS'")
        if problem in ('fees','quantity'):
            obs=json.loads(f.t.row('order-one')['observation_json'])
            obs['costs_reconciled' if problem=='fees' else 'exit_quantity']=False if problem=='fees' else 1
            c.execute('UPDATE native_orders SET observation_json=?',(json.dumps(obs),))
        if problem=='local':c.execute("UPDATE orders SET state='OPEN'")
        if problem=='queue':c.execute('INSERT INTO native_exit_requests VALUES(?,?,?,?)',(b,'order-one','pending',0))
        if problem=='alert':c.execute('INSERT INTO native_operator_alerts VALUES(?,?,?,?,?)',(b,'order-one','pending',0,0))
        if problem=='phase':c.execute('UPDATE native_order_phase SET recovery=1')
    if problem=='stale':f.now+=31
    r=dict(verified=False,missing=['fee_adjustment_pending']) if problem=='account' else dict(verified=True,missing=[])
    with patch('directional_account_reconciliation.state',return_value=r):
        f.t.post_trade.check();s=f.t.post_trade.state()
        assert s['state']=='PENDING' and expected in s['reasons']
        since=s['blocked_since'];f.now+=121
        # Restart preserves the timer and all unresolved broker evidence.
        restarted=native.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
        restarted.post_trade.check();s=restarted.post_trade.state()
        assert s['state']=='BLOCKED' and s['level']=='critical' and s['blocked_since']==since
        assert expected in s['reasons']
        if problem=='action':assert restarted.entry_blocker()=='broker:native_action_outcome_unknown'


def test_watchdog_stall_never_shows_cached_settled_as_current(closed):
    f=closed;f.t.post_trade.check();f.now+=46
    assert f.t.post_trade.state()['state']=='STALE'
    assert f.t.post_trade.state()['level']=='critical'


def test_verified_recovery_clears_status_but_not_safety_switches(closed):
    f=closed
    with patch.dict('os.environ',EXEC_KILL_SWITCH='true',LIVE_EXECUTION_ENABLED='false'):
        with f.api.connect() as c:c.execute('INSERT INTO native_exit_requests VALUES(?,?,?,?)',(f.t.binding,'order-one','old',0))
        f.t.post_trade.check();assert f.t.post_trade.state()['state']=='PENDING'
        f.t.drain_exits(f.sync.snapshot())  # Existing proven terminal-order cleanup.
        f.t.post_trade.check();assert f.t.post_trade.state()['state']=='SETTLED'
        assert native.execution_safety.entry_blocker()=='live_execution_disabled'


def test_active_position_is_not_mislabelled_post_trade_failure(closed):
    f=closed;f.snapshot(position=1)
    with f.api.connect() as c:c.execute("UPDATE native_orders SET state='OPEN'")
    f.t.post_trade.check()
    assert f.t.post_trade.state()['state']=='ACTIVE'
    assert f.t.post_trade.state()['level']=='info'
    assert not f.t.post_trade.state()['authorizes_entries']


def test_position_after_local_closure_is_an_unresolved_mismatch(closed):
    f=closed;f.snapshot(position=1)
    f.t.post_trade.check()
    assert f.t.post_trade.state()['state']=='PENDING'
    assert 'broker_exposure_or_orders_remain' in f.t.post_trade.state()['reasons']


def test_no_fill_is_checked_and_local_mismatch_remains_visible(closed):
    f=closed
    with f.api.connect() as c:
        c.execute("UPDATE native_orders SET state='NO_FILL'")
        c.execute("UPDATE orders SET state='NO_FILL'")
    f.t.post_trade.check();assert f.t.post_trade.state()['state']=='SETTLED'
    with f.api.connect() as c:c.execute("UPDATE orders SET state='OPEN'")
    f.t.post_trade.check()
    assert 'local_no_fill_state_mismatch' in f.t.post_trade.state()['reasons']


def test_late_fill_with_unresolved_close_is_never_settled(closed):
    f=closed;f.snapshot(position=1)
    with f.api.connect() as c:c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',(f.t.binding,'unknown','order-one','close','UNKNOWN','{}','{}',0))
    f.t.post_trade.check();s=f.t.post_trade.state()
    assert s['state']=='PENDING'
    assert 'broker_exposure_or_orders_remain' in s['reasons']
    assert f.t.entry_blocker()=='broker:native_action_outcome_unknown'


def test_checker_failure_is_visible_and_does_not_modify_orders(closed):
    f=closed
    with patch.object(f.t.post_trade,'inspect',side_effect=ValueError('invalid evidence')):
        f.t.post_trade.check()
    assert f.t.post_trade.state()['reasons']==['post_trade_check_failed']
    assert f.t.row('order-one')['state']=='CLOSED'


def test_checker_does_not_clear_other_account_blockers(closed):
    f=closed
    with f.api.connect() as c:c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',('other','unknown','foreign','close','UNKNOWN','{}','{}',0))
    f.t.post_trade.check();assert f.t.post_trade.state()['state']=='SETTLED'
    with f.api.connect() as c:assert c.execute("SELECT state FROM native_actions WHERE binding='other'").fetchone()[0]=='UNKNOWN'


def test_reconciler_always_publishes_post_trade_check_even_if_cost_reader_fails(closed):
    f=closed
    with patch.object(f.t,'reconcile_costs',side_effect=ValueError('cost read failed')):
        with pytest.raises(ValueError):f.t.reconcile(f.sync.snapshot())
    assert f.t.post_trade.state()['state']!='STARTING'


def test_guard_health_exposes_watchdog_problem(monkeypatch):
    import guardrails as guard
    monkeypatch.setattr(native,'selected',lambda:True)
    monkeypatch.setattr(native,'status',lambda:dict(post_trade=dict(level='critical',summary='Unresolved closing action')))
    monkeypatch.setattr(guard,'_health_alert',lambda *a:None)
    monkeypatch.setattr(guard,'_set_state',lambda *a:None)
    result=guard.health(market_open=False)
    check=next(c for c in result['checks'] if c['name']=='post_trade')
    assert check['level']=='critical' and not check['ok']
