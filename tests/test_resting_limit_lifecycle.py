"""Adversarial lifecycle acceptance. All HTTP and AI responses are MOCKS."""
import copy
import json
from unittest.mock import Mock
import pytest
import builder_crosstrade as native
import test_builder_crosstrade as fixture

@pytest.fixture
def f():
    x=fixture.NativeTests();x.setUp()
    x.order.update(quantity=1,target_price=30020.);x.prepare()
    payload=x.payload;x.payload=lambda:dict(payload(),quantity=x.order['quantity'])
    yield x
    x.doCleanups()

def submit(f):
    result=f.submit()
    for life in f.life.values():life['version']['orderQty']=f.order['quantity']
    f.accept(f.order,dict(state='SENT',quantity=f.order['quantity'],broker=result))
    return result

def fill(f,price=30000.,qty=1,terminal=True):
    f.life[11]['order']['ordStatus']='Filled' if terminal else 'PartFilled'
    for oid in (12,13):
        if oid in f.life:
            f.life[oid]['order']['ordStatus']='Working';f.life[oid]['version']['orderQty']=qty
    sign=1 if f.order['direction']=='LONG' else -1
    event=f.fill(101,11,qty,price,'Buy' if sign==1 else 'Sell')
    return f.snapshot([event],sign*qty)

def phase(f):
    with f.api.connect() as db:return dict(db.execute('SELECT * FROM native_order_phase').fetchone())


def test_timeout_reconciles_then_retries_same_parent_after_restart(f):
    submit(f);f.now+=61;f.timeout=True;f.t.reconcile(f.snapshot())
    assert len(f.posts)==2 and phase(f)['recovery']==1
    f.timeout=False
    restarted=native.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    f.now+=10;restarted.reconcile(f.snapshot());assert len(f.posts)==2
    f.now+=21;restarted.reconcile(f.snapshot());assert len(f.posts)==3
    assert all(path.endswith('/11/cancel') for path,body in f.posts[1:])
    assert sum(p.endswith('/orders/place') for p,b in f.posts)==1
    for oid in (11,12,13):f.life[oid]['order']['ordStatus']='Canceled'
    f.now+=1;restarted.reconcile(f.snapshot());f.now+=16;restarted.reconcile(f.snapshot())
    assert restarted.row('order-one')['state']=='NO_FILL' and phase(f)['phase']=='TERMINAL'
    assert restarted.entry_blocker() is None


def test_timeout_followed_by_fill_never_retries_cancel_or_loses_position(f):
    submit(f);f.now+=61;f.timeout=True;f.t.reconcile(f.snapshot());f.timeout=False;f.now+=32
    snap=fill(f);f.t.reconcile(snap)
    assert len(f.posts)==2
    assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
    obs=json.loads(f.t.row('order-one')['observation_json'])
    assert obs['late_fill'] and obs['protection_verified'] and phase(f)['phase']=='FILLED'
    assert f.t.entry_blocker() is not None


def test_cancel_ack_precedes_late_fill_and_expired_ai_does_not_hide_exposure(f):
    submit(f);f.now+=61;f.t.reconcile(f.snapshot())
    f.now+=550;f.t.reconcile(fill(f))
    obs=json.loads(f.t.row('order-one')['observation_json'])
    assert obs['setup_expired_fill'] and obs['protection_verified']
    assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
    assert len(f.posts)==2


@pytest.mark.parametrize('missing',['read','ids'])
def test_missing_children_cannot_block_exact_parent_cancel(f,missing):
    submit(f);f.now+=61
    if missing=='ids':
        with f.api.connect() as db:db.execute('UPDATE native_orders SET receipt_json=?',(json.dumps({'orderId':11}),))
        del f.life[12];del f.life[13]
    else:del f.life[13]
    f.t.reconcile(f.snapshot())
    assert any(path.endswith('/11/cancel') for path,body in f.posts)
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)
    assert f.t.entry_blocker()


@pytest.mark.parametrize('direction,price',[('LONG',29999.75),('SHORT',30000.25)])
def test_price_improvement_keeps_approved_bracket_and_records_actual_risk(f,direction,price):
    if direction=='SHORT':f.order.update(direction=direction,stop_price=30010.,target_price=29980.);f.prepare()
    submit(f);f.now+=5;f.t.reconcile(fill(f,price))
    assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
    position=json.loads(f.local()['position_json'])
    assert position['entry_price']==price and position['actual_risk_points']==9.75
    assert position['actual_reward_points']==20.25
    assert position['stop_price']==f.order['stop_price'] and position['target_price']==f.order['target_price']


def test_partial_cancel_preserves_both_protective_children(f):
    f.order['quantity']=2;f.prepare();submit(f);f.now+=5
    snap=fill(f,qty=1,terminal=False);f.t.reconcile(snap)
    f.pending_gate.return_value='account_risk_changed';f.now+=1;f.t.reconcile(f.snapshot(snap['fills'],1))
    assert f.posts[-1][0].endswith('/11/cancel')
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)
    f.life[11]['order']['ordStatus']='Canceled';f.now+=1;f.t.reconcile(f.snapshot(snap['fills'],1))
    obs=json.loads(f.t.row('order-one')['observation_json'])
    assert obs['remaining_quantity']==1 and obs['protection_verified']
    assert f.t.entry_blocker()


@pytest.mark.parametrize('reason',['pending_m1_not_fresh','pending_thesis:original_bracket_touched_before_entry','account_projected_daily_loss_breach','execution_kill_switch','pending_review_expired'])
def test_pending_watchdog_cancels_before_expiry_without_modifying_protection(f,reason):
    submit(f);f.pending_gate.return_value=reason;f.now+=2;f.t.reconcile(f.snapshot())
    assert f.posts[-1][0].endswith('/11/cancel') and len(f.posts)==2
    assert phase(f)['phase']=='CANCEL_PENDING'


def test_disconnect_keeps_recovery_durable_and_restart_adopts_actual_late_position(f):
    submit(f);f.now+=61;original=f.t.request;f.t.request=Mock(side_effect=TimeoutError())
    f.t.reconcile(f.snapshot());assert len(f.posts)==1 and phase(f)['recovery']
    f.now+=600;f.t=native.Transport(f.api,f.sync,request=original,clock=lambda:f.now)
    f.t.reconcile(fill(f));assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
    assert json.loads(f.t.row('order-one')['observation_json'])['protection_verified']


@pytest.mark.parametrize('stop_status',['missing','Rejected'])
def test_unverified_stop_tracks_exposure_and_recovers_without_cancelling_protection(f,stop_status):
    submit(f);f.now+=605;snap=fill(f)
    if stop_status=='missing':del f.life[13]
    else:f.life[13]['order']['ordStatus']=stop_status
    f.t.reconcile(snap)
    assert f.t.entry_blocker()
    assert f.posts[-1][0].endswith('/positions/close')
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)
    with f.api.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM native_operator_alerts WHERE resolved=0').fetchone()[0]
        assert db.execute('SELECT COUNT(*) FROM native_exit_requests').fetchone()[0]
    assert json.loads(phase(f)['exposure_json'])['remaining_quantity']==1


def test_unknown_external_position_after_restart_blocks_and_is_not_adopted(f):
    submit(f);f.now+=5;snap=f.snapshot(position=1)
    restarted=native.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    restarted.reconcile(snap)
    assert restarted.entry_blocker() and f.local()['position_json'] is None
    assert not any(path.endswith('/positions/close') for path,body in f.posts)
    assert json.loads(phase(f)['exposure_json'])['broker_positions']


def test_broker_pending_cancel_does_not_issue_duplicate_request(f):
    submit(f);f.now+=61;f.t.reconcile(f.snapshot());f.life[11]['order']['ordStatus']='PendingCancel'
    f.now+=100;f.t.reconcile(f.snapshot());assert len(f.posts)==2


def test_missing_ids_ack_retains_original_parent_for_cancellation(f):
    original=f.request
    def request(method,path,body=None):
        result=original(method,path,body)
        if path.endswith('/orders/place'):result['response']={'orderId':11}
        return result
    f.t.request=request
    result=f.submit()
    f.life[11]['version']['orderQty']=1
    assert result['submission_unknown'] and json.loads(f.t.row('order-one')['receipt_json'])=={'orderId':11}
    del f.life[12];del f.life[13];f.now+=61;f.t.reconcile(f.snapshot())
    assert any(path.endswith('/11/cancel') for path,body in f.posts)

def test_changed_parent_limit_can_still_be_cancelled_by_proven_identity(f):
    submit(f);f.now+=2;f.life[11]['version']['price']=30001.
    f.t.reconcile(f.snapshot())
    assert any(path.endswith('/11/cancel') for path,body in f.posts)
    assert f.t.entry_blocker() and phase(f)['recovery']


def test_webhook_late_fill_flag_cannot_claim_native_verification(f):
    submit(f);f.now+=605
    body=dict(event='POSITION',event_id='forged',event_ms=int(f.now*1000),order_id='order-one',
        route_id=f.api.route(),contract=f.api.contract(),account_label='Builder 50K',quantity=1,
        fill_ms=int((f.now-1)*1000),entry_price=30000.,position_id='ct:11',direction='LONG',
        exclusive=True,stop_price=29990.,target_price=30020.,native_verified=True)
    with pytest.raises(ValueError,match='invalid fill timestamp'):f.api.broker_event(body)

def test_expired_entry_child_cancel_attempts_are_terminally_reconciled(f):
    submit(f);f.life[11]['order']['ordStatus']='Expired';f.now+=1
    f.t.reconcile(f.snapshot())
    assert len(f.posts)==3
    for oid in (12,13):f.life[oid]['order']['ordStatus']='Canceled'
    f.now+=1;f.t.reconcile(f.snapshot());f.now+=16;f.t.reconcile(f.snapshot())
    assert f.t.row('order-one')['state']=='NO_FILL' and f.t.entry_blocker() is None


def test_concurrent_expiry_pollers_share_durable_cancel_reservation(f):
    from concurrent.futures import ThreadPoolExecutor
    submit(f);f.now+=61
    row=f.t.row('order-one')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:f.t.cancel_owned_order(row,11,'cancel','any'),range(2)))
    assert len(f.posts)==2


def test_pending_callback_cannot_authorize_entry_without_quote_gate(f):
    f.final_gate.return_value='validated_execution_policy_missing'
    f.pending_gate.return_value=None
    result=f.t.submit(f.payload(),'order-one')
    assert not result['sent'] and len(f.posts)==0
    assert phase(f)['phase']=='TERMINAL'

def test_slow_missing_child_does_not_starve_fresh_protective_recovery(f):
    submit(f);f.now+=5;snap=fill(f)
    original=f.t.request
    def slow(method,path,body=None):
        if method=='GET' and '/orders/13' in path:
            f.now+=4
            raise TimeoutError('MOCK CHILD TIMEOUT')
        return original(method,path,body)
    f.t.request=slow
    f.t.reconcile(snap)
    assert f.posts[-1][0].endswith('/positions/close')
    assert not any(path.endswith('/12/cancel') for path,body in f.posts)


def test_native_deadline_never_outlives_original_ai_approval(f,monkeypatch):
    import directional_ai_review
    monkeypatch.setattr(directional_ai_review,'check',lambda *args:(None,dict(expires_at=f.now+10,review_id='MOCK')))
    submit(f)
    assert f.t.row('order-one')['expiry_ms']==int((f.now+10)*1000)

def test_orphan_cleanup_never_cancels_working_protection_from_stale_flat_poll(f):
    submit(f);f.now+=2;f.life[11]['order']['ordStatus']='Canceled'
    for oid in (12,13):f.life[oid]['order']['ordStatus']='Working'
    f.t.reconcile(f.snapshot())
    assert len(f.posts)==1 and f.t.entry_blocker()
    assert phase(f)['recovery']


def test_fill_arriving_during_orphan_cleanup_preserves_suspended_children(f):
    submit(f);f.now+=2;f.life[11]['order']['ordStatus']='Canceled';old=f.snapshot()
    # The next authenticated account read discovers exposure after the old poll.
    f.values['/accounts/Builder%20account/positions']=[dict(accountId=2214,contractId=100,netPos=1)]
    f.t.reconcile(old)
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)
    assert f.t.entry_blocker()
