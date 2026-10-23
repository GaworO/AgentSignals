"""V1 protection recovery only. All broker operations are injected mock IO."""
import copy
import json
from unittest.mock import Mock
import pytest
import builder_crosstrade as broker
from test_crosstrade_phase2b import sim


@pytest.mark.parametrize('payload_fields',[
    {'tif':'ioc'},{'timeInForce':'IOC'},{'tif':'day','timeInForce':'ioc'},
    {'tif':'ioc','timeInForce':'gtd'},{'timeInForce':'fok'},{'tif':'gtc'}])
def test_ioc_and_aliases_cannot_become_resting_entry(sim,payload_fields):
    p=sim.payload();p.update(payload_fields)
    with pytest.raises(broker.TransportError,match='DIRECTIONAL_LIMIT_TIF_UNSUPPORTED'):
        sim.t.submit(p,'order-one')
    assert sim.posts==[]


@pytest.mark.parametrize('qty',[1,2])
@pytest.mark.parametrize('leg',[12,13])
def test_zero_size_child_is_owned_but_never_protection_and_recovers(sim,qty,leg):
    f=sim;f.submit();f.open(qty);f.now+=1
    f.life[leg]['version']['orderQty']=0
    f.t.reconcile(f.snapshot([f.entry],qty))
    row=f.t.row('order-one');obs=json.loads(row['observation_json'])
    assert not obs['protection_verified'] and row['state']=='RECONCILIATION_REQUIRED'
    assert f.t.entry_blocker()
    with f.api.connect() as db:assert db.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==1
    if qty==1:
        assert f.posts[-1][0].endswith('/11/cancel')
        assert not any(p[0].endswith('/positions/close') for p in f.posts)
        f.life[11]['order']['ordStatus']='Canceled';f.now+=1
        f.t.reconcile(f.snapshot([f.entry],qty))
    assert f.posts[-1][0].endswith('/positions/close')
    assert sum(p[0].endswith('/orders/place') for p in f.posts)==1
    # Restart reuses the recovery/action journal, never duplicates the close.
    n=len(f.posts);f.now+=1
    resumed=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    resumed.reconcile(f.snapshot([f.entry],qty))
    assert len(f.posts)==n


@pytest.mark.parametrize('bad',['disconnect','foreign_account','foreign_contract','broken_oco','missing_lifecycle'])
def test_unverifiable_protection_persists_recovery_without_unsafe_close(sim,bad):
    f=sim;f.submit();f.open();f.now+=1
    real=f.t.request
    if bad=='disconnect':f.t.request=Mock(side_effect=TimeoutError())
    elif bad=='foreign_account':f.life[13]['order']['accountId']=999
    elif bad=='foreign_contract':f.life[13]['order']['contractId']=999
    elif bad=='broken_oco':f.life[13]['order']['ocoId']=123456
    else:f.life[13]['reports']=None
    snap=f.snapshot([f.entry],2);f.t.reconcile(snap)
    assert len(f.posts)==1 and f.t.entry_blocker()
    with f.api.connect() as db:
        assert db.execute('SELECT reason FROM native_exit_requests').fetchone()[0]=='BROKER_PROTECTION_RECONCILIATION_FAILED'
        blocked=db.execute("SELECT payload_json FROM native_audit WHERE kind='EXIT_RECOVERY_BLOCKED'").fetchall()
        assert len(blocked)==1 and json.loads(blocked[0][0])['additional_exposure_blocked']
    f.now+=1;f.t.reconcile(f.snapshot([f.entry],2))
    with f.api.connect() as db:assert db.execute("SELECT count(*) FROM native_audit WHERE kind='EXIT_RECOVERY_BLOCKED'").fetchone()[0]==1
    assert len(f.posts)==1
    if bad=='disconnect':
        # Evidence reconnects after restart: exact owned full position closes once.
        f.now+=1;resumed=broker.Transport(f.api,f.sync,request=real,clock=lambda:f.now)
        resumed.reconcile(f.snapshot([f.entry],2))
        assert f.posts[-1][0].endswith('/positions/close')
        assert sum(p[0].endswith('/positions/close') for p in f.posts)==1


def test_master_write_disable_keeps_recovery_pending_without_mutation(sim,monkeypatch):
    f=sim;f.submit();f.open();f.now+=1;f.life[13]['order']['ordStatus']='Canceled'
    monkeypatch.setenv('LIVE_EXECUTION_ENABLED','false')
    f.t.reconcile(f.snapshot([f.entry],2))
    assert len(f.posts)==1 and f.t.entry_blocker()
    with f.api.connect() as db:
        assert db.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==1
        assert json.loads(db.execute("SELECT payload_json FROM native_audit WHERE kind='EXIT_RECOVERY_BLOCKED'").fetchone()[0])['reason']=='LIVE_EXECUTION_DISABLED'


def test_entry_kill_switch_does_not_disable_existing_protective_recovery(sim,monkeypatch):
    f=sim;f.submit();f.open();f.now+=1;f.life[13]['order']['ordStatus']='Canceled'
    monkeypatch.setenv('EXEC_KILL_SWITCH','true')
    f.t.reconcile(f.snapshot([f.entry],2))
    assert f.posts[-1][0].endswith('/positions/close')
    assert sum(p[0].endswith('/orders/place') for p in f.posts)==1


def test_ack_without_fill_even_with_working_children_never_creates_position(sim):
    f=sim;result=f.submit()
    assert result['sent'] and result['reason']=='native_oso_accepted_not_filled'
    for oid in (12,13):f.life[oid]['order']['ordStatus']='Working'
    f.now+=1;f.t.reconcile(f.snapshot())
    assert f.t.row('order-one')['state']=='WAITING_BROKER_FILL'
    assert f.local()['position_json'] is None
    assert f.t.entry_blocker()


def test_mis_sized_working_stop_never_opens_manager_permission(sim):
    f=sim;f.submit();f.open(1);f.life[13]['version']['orderQty']=2;f.now+=1
    f.t.reconcile(f.snapshot([f.entry],1))
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'
    assert f.local()['position_json'] is None
    assert not json.loads(f.t.row('order-one')['observation_json'])['protection_verified']
    assert f.posts[-1][0].endswith('/11/cancel')


def test_recovery_ack_waits_for_fill_terminal_children_and_costs(sim):
    f=sim;f.submit();f.open();f.now+=1;f.life[13]['order']['ordStatus']='Canceled'
    f.t.reconcile(f.snapshot([f.entry],2))
    assert f.posts[-1][0].endswith('/positions/close')
    assert f.t.row('order-one')['state']!='CLOSED'
    f.life[14]=dict(order=dict(id=14,accountId=2214,contractId=100,action='Sell',ordStatus='Working'),
        version=dict(orderId=14,orderQty=2,orderType='Market'),commands=[],reports=[])
    f.now+=1;f.t.reconcile(f.snapshot([f.entry],2))
    assert f.t.row('order-one')['state']!='CLOSED'
    f.life[14]['order']['ordStatus']='Filled';f.life[12]['order']['ordStatus']='Canceled'
    f.now+=2;exit=f.fill(102,14,2,29999.,'Sell')
    f.account['balance'].update(netLiq=48955.15,totalCashValue=48955.15)
    snap=f.snapshot([f.entry,exit]);f.t.reconcile(snap)
    assert f.t.row('order-one')['state']=='CLOSED_PENDING_COSTS' and f.t.entry_blocker()
    f.history=[dict(fillId=x['id'],orderId=x['orderId'],accountId=2214,accountName='Builder account',environment='demo',instrument='MNQ 12-26',
        qty=x['qty'],price=x['price'],action=x['action'],timestamp=x['timestamp'],commission=.6,fees=.6) for x in (f.entry,exit)]
    f.now+=61;f.t.reconcile(f.snapshot([f.entry,exit]))
    assert f.t.row('order-one')['state']=='CLOSED'
    assert json.loads(f.t.row('order-one')['observation_json'])['net_usd']==-6.4
    assert f.t.entry_blocker() is None
    assert sum(p[0].endswith('/positions/close') for p in f.posts)==1
    with f.api.connect() as db:assert db.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==0


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('filled_leg',[12,13])
def test_partial_protective_fill_uses_unfilled_quantity_and_checks_sibling(sim,direction,filled_leg):
    f=sim
    if direction=='SHORT':f.order.update(direction='SHORT',stop_price=30010.,target_price=29970.);f.prepare()
    f.submit();f.open();f.now+=1
    f.life[filled_leg]['order']['ordStatus']='PartFilled'
    sibling=13 if filled_leg==12 else 12
    f.life[sibling]['version']['orderQty']=1
    partial=f.fill(102,filled_leg,1,f.order['target_price' if filled_leg==12 else 'stop_price'],'Sell' if direction=='LONG' else 'Buy')
    snap=f.snapshot([f.entry,partial],1 if direction=='LONG' else -1)
    f.t.reconcile_one(f.t.row('order-one'),snap)
    obs=json.loads(f.t.row('order-one')['observation_json'])
    assert obs['remaining_quantity']==1 and obs['protection_verified']
    assert obs['protective_remaining_quantities']=={'12':1,'13':1}
    assert len(f.posts)==1
    # A sibling that was NOT resized is over-sized protection and must block.
    f.life[sibling]['version']['orderQty']=2;f.now+=1
    f.t.reconcile_one(f.t.row('order-one'),f.snapshot([f.entry,partial],1 if direction=='LONG' else -1))
    assert not json.loads(f.t.row('order-one')['observation_json'])['protection_verified']
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'
    with f.api.connect() as db:assert db.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==1
