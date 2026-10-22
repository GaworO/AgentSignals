import copy
import sqlite3
from unittest.mock import Mock
import pytest
from directional_position_manager import Manager
from directional_management_broker import CrossTradeManagementAdapter,observe_native
import test_builder_crosstrade as protocol


class SimulatedAdapter:
    """Atomic reduction is a simulator capability, NOT a CrossTrade claim."""
    def __init__(self,b):self.b=b;self.sent=[];self.timeout=False
    def read(self,oid):return copy.deepcopy(self.b)
    def require_write(self,op):
        if op not in ('STOP','TARGET','REDUCE','EXIT'):raise ValueError('UNSUPPORTED')
    def submit(self,state,intent):
        self.sent.append(copy.deepcopy(intent))
        if self.timeout:raise TimeoutError()
        return dict(success=True,command_id=77,broker_order_id=14 if intent['operation'] in ('EXIT','REDUCE') else None)


def setup(tmp_path,direction='LONG'):
    path=tmp_path/'state.db';connect=lambda:sqlite3.connect(path)
    m=Manager(connect,'SIMULATOR');z=1 if direction=='LONG' else -1
    order=dict(order_id='one',strategy='AB_DIRECTIONAL',direction=direction,contract='MNQZ2026',quantity=2,
               entry_price=100.,stop_price=100-z*10,target_price=100+z*20)
    m.register(order,dict(orderId=11,oso1Id=12,oso2Id=13))
    b=dict(binding='SIMULATOR',contract='MNQZ2026',complete=True,observed_ms=120000,
        net_position=2*z,average_entry=100.,entry_terminal=True,foreign_activity=False,
        fills=[dict(id=101,order_id=11,quantity=2,price=100.,side=z,ms=50000,fees=1.9)],
        protective_orders=[dict(id=12,kind='TARGET',quantity=2,price=100+z*20,status='Working',parent=11,oco=9,side=-z,confirmed=True),
                           dict(id=13,kind='STOP',quantity=2,price=100-z*10,status='Working',parent=11,oco=9,side=-z,confirmed=True)],command_reports=[])
    assert m.reconcile('one',b,120000)['state']=='PROTECTED'
    return m,b,SimulatedAdapter(b)


def event(direction='LONG',kind='CLOSE'):
    return dict(contract='MNQZ2026',confirmed=True,source='SIMULATED_CLOSED_M1',open_ms=60000,closed_ms=120000,
                close=111. if direction=='LONG' else 89.,high=112.,low=88.,kind=kind)


def propose(m,b,opts=None,direction='LONG',ev=None):
    q=dict(contract='MNQZ2026',bid=111. if direction=='LONG' else 88.75,ask=111.25 if direction=='LONG' else 89.,
           as_of_ms=b['observed_ms'],execution_eligible=True,timestamp_semantics_verified=True)
    return m.on_event('one',ev or event(direction),q,dict(binding='SIMULATOR',allowed=True,checked_ms=b['observed_ms']),opts or {},b['observed_ms'],max_quote_age_ms=1000,max_event_age_ms=60000)


def level(px,**kw):return dict(enabled=True,source='RESEARCH_ONLY',price=px,known_ms=120000,expires_ms=180000,**kw)


def exit_fill(b,qty,px,oid=12,fee=.95):
    z=1 if b['fills'][0]['side']==1 else -1
    b['observed_ms']+=100;b['net_position']-=z*qty
    b['fills'].append(dict(id=100+len(b['fills'])+1,order_id=oid,quantity=qty,price=px,side=-z,ms=b['observed_ms'],fees=fee))
    for leg in b['protective_orders']:
        leg['quantity']=abs(b['net_position']);leg['status']='Working' if b['net_position'] else 'Filled' if leg['id']==oid else 'Canceled'


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('winning',[True,False])
def test_entry_full_target_or_stop(tmp_path,direction,winning):
    m,b,a=setup(tmp_path,direction);z=1 if direction=='LONG' else -1
    exit_fill(b,2,100+z*(20 if winning else -10),12 if winning else 13,1.9)
    s=m.reconcile('one',b,b['observed_ms'])
    assert s['state']=='CLOSED' and s['remaining_quantity']==0 and s['fees']==3.8
    assert s['realized_net_usd']==pytest.approx((80 if winning else -40)-3.8)


@pytest.mark.parametrize('runner',[False,True])
def test_partial_to_runner_or_remaining_stop(tmp_path,runner):
    m,b,a=setup(tmp_path)
    p=propose(m,b,{'partial':level(110.,quantity=1)})
    i=m.request('one',p,a,120000)
    assert i['status']=='ACK' and m.get('one')['remaining_quantity']==2
    assert m.get('one')['state']=='REDUCE_PENDING'
    exit_fill(b,1,110.,14)
    assert m.reconcile('one',b,b['observed_ms'])['state']=='PROTECTED'
    assert m.get('one')['partial_done']
    if runner:
        p=propose(m,b,{'runner':level(130.)});i=m.request('one',p,a,b['observed_ms'])
        assert not m.get('one')['runner']
        b['protective_orders'][0]['price']=130.;b['observed_ms']+=100
        b['command_reports']=[dict(command_id=77,confirmed=True,rejected=False)]
        assert m.reconcile('one',b,b['observed_ms'])['state']=='RUNNER'
    exit_fill(b,1,130. if runner else 90.,12 if runner else 13)
    assert m.reconcile('one',b,b['observed_ms'])['state']=='CLOSED'
    assert len(a.sent)==(2 if runner else 1)


def test_trailing_ack_is_not_confirmation_and_restart(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    i=m.request('one',p,a,120000);assert i['status']=='ACK'
    resumed=Manager(m.connect,m.binding);assert resumed.get('one')['last_stop']==90.
    # Version alone is not proof; broker report for exact command is mandatory.
    b['protective_orders'][1]['price']=105.;b['observed_ms']+=100
    assert resumed.reconcile('one',b,b['observed_ms'])['state']=='RECONCILIATION_REQUIRED'
    b['command_reports']=[dict(command_id=77,confirmed=True,rejected=False)];b['observed_ms']+=100
    s=resumed.reconcile('one',b,b['observed_ms']);assert s['last_stop']==105. and s['current_stop_risk_usd']==0
    assert len(a.sent)==1


def test_rejected_stop_and_manual_modification(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    m.request('one',p,a,120000);b['command_reports']=[dict(command_id=77,rejected=True,confirmed=False)];b['observed_ms']+=100
    s=m.reconcile('one',b,b['observed_ms']);assert s['state']=='RECONCILIATION_REQUIRED'
    assert s['last_stop']==90 and s['reason']=='BROKER_COMMAND_REJECTED'


@pytest.mark.parametrize('opts,ev,reason',[
    ({'invalidation':level(112.)},event(),'STRUCTURAL_INVALIDATION'),
    ({'session_end':{'enabled':True,'at_ms':120000}},event(),'SESSION_END')])
def test_exit_rules_are_explicit_and_never_mark_ack_closed(tmp_path,opts,ev,reason):
    m,b,a=setup(tmp_path);assert propose(m,b)['action']=='HOLD'
    p=propose(m,b,opts,ev=ev);assert p['reason']==reason
    m.request('one',p,a,120000);assert m.get('one')['state']=='EXIT_PENDING'
    exit_fill(b,2,111.,14,1.9);assert m.reconcile('one',b,b['observed_ms'])['state']=='CLOSED'


def test_partial_entry_protected_but_management_waits(tmp_path):
    m,b,a=setup(tmp_path);b['fills'][0]['quantity']=1;b['net_position']=1;b['entry_terminal']=False
    # New account/store represents a first partial, not revision of an old fill.
    m=Manager(lambda:sqlite3.connect(tmp_path/'partial.db'),'SIMULATOR');m.register(dict(order_id='one',strategy='AB_DIRECTIONAL',direction='LONG',contract='MNQZ2026',quantity=2,entry_price=100.,stop_price=90.,target_price=120.),dict(orderId=11,oso1Id=12,oso2Id=13))
    for l in b['protective_orders']:l['quantity']=1
    assert m.reconcile('one',b,120000)['state']=='PROTECTED'
    assert propose(m,b,{'partial':level(110.,quantity=1)})['reason']=='ENTRY_STILL_WORKING'
    b['fills'].append(dict(id=102,order_id=11,quantity=1,price=100.,side=1,ms=120100,fees=.95));b['net_position']=2;b['observed_ms']=120100;b['entry_terminal']=True
    assert m.reconcile('one',b,120100)['state']=='RECONCILIATION_REQUIRED' # old one-contract protection
    for l in b['protective_orders']:l['quantity']=2
    assert m.reconcile('one',b,120100)['state']=='PROTECTED'


@pytest.mark.parametrize('change,reason',[
    ('position','BROKER_LOCAL_POSITION_MISMATCH'),('stop_qty','PROTECTION_QUANTITY_OR_LINK_MISMATCH'),
    ('manual','UNEXPECTED_MANUAL_INTERVENTION'),('stop_price','STOP_LOOSENED'),
    ('target_price','UNEXPECTED_MANUAL_ORDER_CHANGE'),('account','ACCOUNT_OR_CONTRACT_MISMATCH'),
    ('stale','BROKER_SNAPSHOT_STALE_OR_INCOMPLETE')])
def test_inconsistent_evidence_blocks(tmp_path,change,reason):
    m,b,a=setup(tmp_path);b['observed_ms']+=100
    if change=='position':b['net_position']=1
    if change=='stop_qty':b['protective_orders'][1]['quantity']=3
    if change=='manual':b['foreign_activity']=True
    if change=='stop_price':b['protective_orders'][1]['price']=89.
    if change=='target_price':b['protective_orders'][0]['price']=130.
    if change=='account':b['binding']='FOREIGN'
    now=b['observed_ms']+4000 if change=='stale' else b['observed_ms']
    s=m.reconcile('one',b,now);assert s['state']=='RECONCILIATION_REQUIRED' and s['reason']==reason


def test_duplicate_events_and_unknown_request_not_resent(tmp_path):
    m,b,a=setup(tmp_path);s=m.get('one');assert m.reconcile('one',copy.deepcopy(b),120000)==s
    p=propose(m,b,{'partial':level(110.,quantity=1)});a.timeout=True
    assert m.request('one',p,a,120000)['status']=='UNKNOWN'
    resumed=Manager(m.connect,m.binding)
    with pytest.raises(ValueError):resumed.request('one',p,a,120000)
    assert len(a.sent)==1


def test_no_future_candle_unverified_quote_or_stale_risk(tmp_path):
    m,b,a=setup(tmp_path);e=event();e['closed_ms']=120001
    assert propose(m,b,ev=e)['action']=='BLOCK'
    p=propose(m,b,{'partial':level(110.,quantity=1)});p['quote']['execution_eligible']=False
    with pytest.raises(ValueError):m.request('one',p,a,120000)
    assert not a.sent
    assert propose(m,b,{'trail':level(89.)},ev=event(kind='CONFIRMED_SWING'))['action']=='BLOCK'


def test_real_adapter_cannot_send_even_when_live_flags_true(tmp_path,monkeypatch):
    m,b,a=setup(tmp_path);t=Mock();t.sync.name='test account';adapter=CrossTradeManagementAdapter(t)
    monkeypatch.setenv('LIVE_EXECUTION_ENABLED','true');monkeypatch.setenv('EXEC_KILL_SWITCH','false')
    p=propose(m,b,{'partial':level(110.,quantity=1)})
    with pytest.raises(ValueError,match='PHASE3B'):m.request('one',p,adapter,120000)
    t.request.assert_not_called()
    for intent in [dict(operation='STOP',price=105),dict(operation='REDUCE',quantity=1),dict(operation='QUANTITY',quantity=1,broker_order_id=13)]:
        plan=adapter.operation_plan(m.get('one'),intent);assert plan['allowed'] is False


def test_existing_native_reconciler_integration_is_read_only():
    f=protocol.NativeTests();f.setUp()
    try:
        f.submit();snap=f.open();f.t.reconcile(snap)
        m=Manager(f.api.connect,f.t.binding);s=m.get('order-one')
        assert s and s['state']=='PROTECTED',s
        assert len(f.posts)==1
        resumed=Manager(f.api.connect,f.t.binding);assert resumed.get('order-one')==s
    finally:f.doCleanups()


def test_packet_is_atomic_when_stop_confirmed_but_target_invalid(tmp_path):
    m,b,a=setup(tmp_path)
    p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    intent=m.request('one',p,a,120000)
    b['protective_orders'][1]['price']=105.
    b['protective_orders'][0]['price']=130. # Unowned target change in same packet.
    b['command_reports']=[dict(command_id=77,confirmed=True,rejected=False)]
    b['observed_ms']+=100
    s=m.reconcile('one',b,b['observed_ms'])
    assert s['state']=='RECONCILIATION_REQUIRED' and s['last_stop']==90.
    with m.connect() as c:
        assert c.execute('SELECT status FROM ict_management_intents WHERE intent_id=?',(intent['id'],)).fetchone()[0]=='ACK'


def test_wrong_order_types_are_reconciliation_failure(tmp_path):
    m,b,a=setup(tmp_path);b['protective_orders'][1]['kind']='MARKET';b['observed_ms']+=100
    s=m.reconcile('one',b,b['observed_ms'])
    assert s['reason']=='PROTECTIVE_ORDER_TYPES_UNKNOWN' and s['state']=='RECONCILIATION_REQUIRED'


def test_stale_account_event_and_expired_rule_cannot_submit(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'partial':level(110.,quantity=1)})
    p['risk']['checked_ms']=116999
    with pytest.raises(ValueError):m.request('one',p,a,120000)
    p=propose(m,b,{'partial':level(110.,quantity=1)})
    p['event']['closed_ms']=59999;p['event']['open_ms']=0
    with pytest.raises(ValueError):m.request('one',p,a,120000)
    expired=level(110.,quantity=1);expired['expires_ms']=119999
    assert propose(m,b,{'partial':expired})['action']=='HOLD'
    assert not a.sent


def test_unknown_timestamps_and_levels_fail_closed(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'partial':level(110.,quantity=1)})
    p['risk']['checked_ms']=None
    with pytest.raises(ValueError):m.request('one',p,a,120000)
    unknown=level(110.,quantity=1);unknown['known_ms']=None
    assert propose(m,b,{'partial':unknown})['action']=='HOLD'
    unknown.update(known_ms=120000,source='UNKNOWN')
    assert propose(m,b,{'partial':unknown})['action']=='HOLD'
    b['observed_ms']=None
    assert m.reconcile('one',b,120000)['state']=='RECONCILIATION_REQUIRED'
    assert not a.sent


def test_setup_and_strategy_ownership_cannot_be_rebound(tmp_path):
    m,b,a=setup(tmp_path);frozen=m.get('one')['frozen'];order=dict(frozen['order'])
    order['order_id']='another'
    with pytest.raises(ValueError,match='ALREADY_OWNED'):m.register(order,frozen['broker_ids'])
    order['strategy']='A/B'
    with pytest.raises(ValueError,match='DIRECTIONAL_ONLY'):m.register(order,frozen['broker_ids'])
    p=propose(m,b,{'partial':level(110.,quantity=1)});p['setup_id']='another'
    with pytest.raises(ValueError,match='SETUP_MISMATCH'):m.request('one',p,a,120000)
    assert not a.sent


def test_closed_accounting_revisions_reach_observer_without_new_order():
    f=protocol.NativeTests();f.setUp()
    try:
        f.submit();f.t.reconcile(f.open())
        snap,net=f.complete();f.t.reconcile(snap)
        m=Manager(f.api.connect,f.t.binding);s=m.get('order-one')
        assert s['state']=='CLOSED' and s['realized_net_usd']==pytest.approx(net)
        original_fee=s['fees']
        f.now+=61
        for fee in f.history:fee['fees']+=1.
        snap=f.snapshot(snap['fills']);f.t.reconcile(snap)
        assert m.get('order-one')['realized_net_usd'] is None # Revised costs disagree with cash.
        f.now+=61
        f.account['balance'].update(totalCashValue=f.account['balance']['totalCashValue']-2.,netLiq=f.account['balance']['netLiq']-2.)
        snap=f.snapshot(snap['fills']);f.t.reconcile(snap)
        updated=m.get('order-one')
        assert updated['state']=='CLOSED' and updated['fees']==pytest.approx(original_fee+2.)
        assert updated['realized_net_usd']==pytest.approx(net-2.)
        assert len(f.posts)==1
    finally:f.doCleanups()


def test_existing_native_close_fill_is_owned_without_new_manager_action():
    f=protocol.NativeTests();f.setUp()
    try:
        f.submit();snap=f.open();f.t.reconcile(snap)
        f.t.action('order-one','close','existing-guard-exit',snapshot=snap)
        f.life[14]=dict(order=dict(id=14,accountId=2214,contractId=100,action='Sell',ordStatus='Filled'),
            version=dict(orderId=14,orderQty=2,orderType='Market'),commands=[],reports=[])
        f.life[12]['order']['ordStatus']='Canceled';f.life[13]['order']['ordStatus']='Canceled'
        f.now+=5
        snap=f.snapshot([f.entry,f.fill(102,14,2,30005.,'Sell')]);f.t.reconcile(snap)
        s=Manager(f.api.connect,f.t.binding).get('order-one')
        assert s['state']=='CLOSED' and s['remaining_quantity']==0,s
        assert s['realized_gross_usd']==20. and s['realized_net_usd'] is None
        assert len(f.posts)==2 # Original entry + original Guard's mocked close only.
    finally:f.doCleanups()
