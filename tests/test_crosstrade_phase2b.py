"""Phase 2B acceptance cases. All broker writes are in-memory simulations."""
import copy
import json
from unittest.mock import Mock, MagicMock
import pytest
import builder_crosstrade as broker
import directional_market_data as quotes
import directional_thesis as thesis
import directional_account_limits as allowances
import execution_validation as gate
import test_builder_crosstrade as protocol
from test_execution_readiness import evidence
from test_directional_completion import allowance_fixture, quote, structural_fixture
from test_directional_risk_revalidation import native


@pytest.fixture
def sim():
    f=protocol.NativeTests();f.setUp()
    try:yield f
    finally:f.doCleanups()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_adverse_quote_rejected_even_when_three_r_and_spread_allow_it(direction):
    a=evidence();a[0].update(direction=direction,stop_price=90 if direction=='LONG' else 110,
                            target_price=130 if direction=='LONG' else 70)
    a[4]['setup']=dict(a[0]);a[1].update(bid=100 if direction=='LONG' else 99.75,
                                      ask=100.25 if direction=='LONG' else 100)
    assert gate.evaluate(*a)=='original_limit_exceeded'


@pytest.mark.parametrize('key',['execution_eligible','timestamp_semantics_verified'])
def test_no_implicit_quote_attestation(key):
    a=evidence();a[1].pop(key)
    assert gate.evaluate(*a)=='quote_source_unverified'


def test_nt8_shape_alone_does_not_verify_timestamp_or_venue():
    raw=quote();a=evidence();p=copy.deepcopy(a[5]);p.pop('quote_verification')
    assert quotes.inspect_quote(raw,'MNQZ2026',1000000,1000)['feed_candidate']
    with pytest.raises(ValueError,match='suitability_unverified'):quotes.normalize(raw,a[0],p,1000000)
    assert quotes.normalize(raw,a[0],a[5],1000000)['execution_eligible']
    for change in ({'contract':'MNQH2027'},{'venue_bbo':False},{'timestamp_semantics':'last_trade'}):
        p=copy.deepcopy(a[5]);p['quote_verification'].update(change)
        with pytest.raises(ValueError):quotes.normalize(raw,a[0],p,1000000)


def test_source_switch_is_observed_never_promoted(tmp_path):
    from test_directional_quotes import collective
    raw=collective();clock=[1000.]
    c=quotes.QuoteClient(clock=lambda:clock[0],reader=lambda _:raw,path=tmp_path/'q.db')
    c.fetch('MNQZ2026');clock[0]+=3
    raw.update(source='drafted',epoch=1003)
    result=c.fetch('MNQZ2026')
    assert result['source_changed'] and result['previous_source']=='collective'
    assert not result['allowed'] and not result['quote']['execution_eligible']


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_zero_quantity_suspended_children_partial_fill_and_restart(sim,direction):
    f=sim
    if direction=='SHORT':f.order.update(direction='SHORT',stop_price=30010.,target_price=29970.);f.prepare()
    f.submit()
    for oid in (12,13):f.life[oid]['version']['orderQty']=0
    f.t.reconcile_one(f.t.row('order-one'),f.snapshot())
    assert f.t.row('order-one')['state']=='WAITING_BROKER_FILL'
    assert f.local()['position_json'] is None
    snap=f.open(1)
    assert f.t.row('order-one')['state']=='PARTIAL_OPEN'
    assert json.loads(f.t.row('order-one')['observation_json'])['protection_verified'] is True
    resumed=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    resumed.reconcile_one(resumed.row('order-one'),snap)
    assert resumed.row('order-one')['state']=='PARTIAL_OPEN' and len(f.posts)==1


def test_working_stop_zero_qty_is_not_protection(sim):
    f=sim;f.submit();snap=f.open()
    f.life[13]['version']['orderQty']=0
    f.now+=1;f.t.reconcile(f.snapshot([f.entry],2))
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'
    assert not json.loads(f.t.row('order-one')['observation_json'])['protection_verified']
    assert f.posts[-1][0].endswith('/positions/close')
    assert f.t.entry_blocker()


def test_expiry_actively_cancels_once_and_ack_is_not_terminal(sim):
    f=sim;f.submit();f.now+=61;f.t.reconcile(f.snapshot())
    assert len(f.posts)==2 and f.posts[-1][0].endswith('/11/cancel')
    assert f.t.row('order-one')['state']=='WAITING_BROKER_FILL'
    with f.api.connect() as c:assert c.execute('SELECT state FROM native_actions').fetchone()[0]=='ACKNOWLEDGED'
    f.now+=1;resumed=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    resumed.reconcile(f.snapshot());assert len(f.posts)==2
    for oid in (11,12,13):f.life[oid]['order']['ordStatus']='Canceled'
    f.now+=1;resumed.reconcile(f.snapshot());f.now+=16;resumed.reconcile(f.snapshot())
    assert resumed.row('order-one')['state']=='NO_FILL'
    with f.api.connect() as c:assert c.execute('SELECT state FROM native_actions').fetchone()[0]=='CONFIRMED'


def test_missing_stop_queues_owned_emergency_exit_and_never_replaces_bracket(sim):
    f=sim;f.submit();f.open();f.life[13]['order']['ordStatus']='Canceled';f.now+=1
    f.t.reconcile(f.snapshot([f.entry],2))
    assert len(f.posts)==2 and f.posts[-1][0].endswith('/positions/close')
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'
    with f.api.connect() as c:assert c.execute('SELECT reason FROM native_exit_requests').fetchone()[0]=='PROTECTION_OR_LIMIT_NOT_VERIFIED'
    assert not json.loads(f.t.row('order-one')['observation_json'])['protection_verified']


@pytest.mark.parametrize('bad',['account','contract','disconnect','position','duplicate_fill'])
def test_broker_inconsistency_never_preserves_open_permission(sim,bad):
    f=sim;f.submit();f.open();f.now+=1
    snap=f.snapshot([f.entry],2)
    if bad=='account':f.life[13]['order']['accountId']=999
    elif bad=='contract':f.life[13]['order']['contractId']=999
    elif bad=='disconnect':f.t.request=Mock(side_effect=TimeoutError())
    elif bad=='position':snap['positions'][0]['netPos']=1
    else:snap['fills'].append(dict(f.entry,price=29999.75))
    f.t.reconcile(snap)
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'
    assert f.t.entry_blocker() and len(f.posts)==1


def test_duplicate_callbacks_and_full_open_restart_do_not_duplicate_orders(sim):
    f=sim;f.submit();snap=f.open()
    resumed=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    for _ in range(3):resumed.reconcile_one(resumed.row('order-one'),snap)
    assert resumed.row('order-one')['state']=='OPEN' and len(f.posts)==1
    with f.api.connect() as c:assert c.execute('SELECT count(*) FROM broker_events').fetchone()[0]==1


@pytest.mark.parametrize('flag,value',[('V3_ENTRY_ARMED','0'),('AB_V3_MODE','SHADOW'),
    ('AB_DIRECTIONAL_LIVE_LONG','0'),('EXEC_STRATEGY_POLICY','SHADOW_ALL'),
    ('EXEC_STRATEGY_POLICY','INVALID'),('EXEC_STRATEGY_POLICY','LEGACY'),('EXEC_KILL_SWITCH','true')])
def test_direct_native_route_cannot_bypass_permissions(sim,monkeypatch,flag,value):
    monkeypatch.setenv(flag,value)
    with pytest.raises(broker.TransportError):sim.submit()
    assert sim.posts==[]


def test_flags_changed_during_final_read_prevent_post(sim,monkeypatch):
    def delayed(*a):monkeypatch.setenv('V3_ENTRY_ARMED','0')
    sim.final_gate.side_effect=delayed
    result=sim.t.submit(sim.payload(),'order-one')
    assert not result['sent'] and result['reason']=='DIRECTIONAL_ENTRY_NOT_ARMED' and not sim.posts


def test_external_position_appearing_during_final_risk_read_blocks(native):
    f=native;f.values['/accounts/Builder%20account/positions']=[dict(accountId=2214,contractId=100,netPos=1)]
    assert f.validator(dict(f.order))['reason']=='broker:account_has_positions_or_orders'
    f.guard_check.assert_not_called()


@pytest.mark.parametrize('change,reason',[
    ('missing','account_wide_controls_unknown'),('stale','account_wide_controls_stale'),
    ('loss','account_wide_one_loss_stop'),('margin','account_margin_or_exposure_limit'),
    ('daily','account_provider_daily_allowance_exceeded'),('fees','account_wide_controls_unverified'),
    ('balance','account_wide_controls_balance_mismatch'),('streak','account_wide_loss_streak_stop')])
def test_unknown_accountwide_risk_and_costs_block(tmp_path,monkeypatch,change,reason):
    a,p,path=allowance_fixture(tmp_path,monkeypatch);c=p['account_controls']
    if change=='missing':p.pop('account_controls')
    elif change=='stale':c['observed_at_ms']-=30001
    elif change=='loss':c['losses_today']=1
    elif change=='margin':c['margin_max_new_micro_contracts']=1
    elif change=='daily':c['remaining_daily_loss_usd']=1
    elif change=='fees':c['fees_reconciled']=False
    elif change=='balance':c['cash']+=1
    else:c.update(consecutive_losses=3,last_loss_closed_ms=a[3]-1)
    path.write_text(json.dumps(p));assert allowances.check(*a)==reason


def test_fresh_context_is_published_before_allow_is_reused(monkeypatch):
    order,source,rows,now=structural_fixture();source['candidate_id']='fixture';order['contract']='MNQZ2026'
    connection=MagicMock();connection.__enter__.return_value=connection
    connection.execute.return_value.fetchone.return_value=source
    connection.execute.return_value.fetchall.return_value=rows
    monkeypatch.setattr(thesis.continuation_shadow,'_connect',lambda:connection)
    monkeypatch.setattr(thesis.ab_v3_live,'connect',lambda:connection)
    monkeypatch.setattr(gate,'policy',lambda:dict(thesis_rule_version=thesis.VERSION))
    service=Mock(trigger='HYBRID');service.packet.return_value=dict(as_of=720)
    events=[]
    monkeypatch.setattr(thesis.ab_ai_context.hybrid_lifecycle,'refresh',lambda *a:events.append('new_structure_published'))
    def annotation(*args):
        assert events==['new_structure_published'];return dict(state='HYBRID_NEW_STRUCTURE_EVENT')
    service.setup_annotation.side_effect=annotation
    monkeypatch.setattr(thesis.ab_ai_context,'INSTANCE',service)
    result=thesis.ThesisValidator(clock=lambda:now/1000)(order)
    assert not result['valid'] and result['reason']=='review_not_allowed'
    service.invalidate.assert_not_called()


def test_ioc_is_not_silently_converted_to_directional_gtd(sim):
    p=sim.payload();p['tif']='ioc'
    with pytest.raises(broker.TransportError,match='LIMIT_TIF_UNSUPPORTED'):sim.t.submit(p,'order-one')
    assert sim.posts==[]


def test_liquidity_objective_changed_after_review_is_blocked(monkeypatch):
    import directional_setup_review
    order,source,rows,now=structural_fixture();source['candidate_id']='fixture';order['contract']='MNQZ2026'
    connection=MagicMock();connection.__enter__.return_value=connection
    connection.execute.return_value.fetchone.return_value=source
    connection.execute.return_value.fetchall.return_value=rows
    monkeypatch.setattr(thesis.continuation_shadow,'_connect',lambda:connection)
    monkeypatch.setattr(thesis.ab_v3_live,'connect',lambda:connection)
    monkeypatch.setattr(gate,'policy',lambda:dict(thesis_rule_version=thesis.VERSION))
    setup=directional_setup_review.describe(source,order['contract']);setup['source_level']=999
    service=Mock(trigger='SETUP');service.packet.return_value=dict(as_of=720)
    service.setup_annotation.return_value=dict(state='ALLOW',setup=setup)
    monkeypatch.setattr(thesis.ab_ai_context,'INSTANCE',service)
    result=thesis.ThesisValidator(clock=lambda:now/1000)(order)
    assert result['reason']=='original_liquidity_or_setup_evidence_changed'


def test_target_filling_partial_cancels_remaining_entry_once(sim):
    f=sim;f.submit();f.open(1);f.now+=1
    exit=f.fill(102,12,1,f.order['target_price'],'Sell')
    f.life[12]['order']['ordStatus']='Filled';f.life[13]['order']['ordStatus']='Canceled'
    snap=f.snapshot([f.entry,exit])
    f.t.reconcile_one(f.t.row('order-one'),snap)
    assert f.t.row('order-one')['state']=='WAITING_ENTRY_CANCEL'
    assert len(f.posts)==2 and f.posts[-1][0].endswith('/11/cancel')
    f.t.reconcile_one(f.t.row('order-one'),snap);assert len(f.posts)==2
    f.life[11]['order']['ordStatus']='Canceled';f.now+=1
    f.t.reconcile_one(f.t.row('order-one'),f.snapshot([f.entry,exit]))
    assert f.t.row('order-one')['state']=='CLOSED_PENDING_COSTS'


def test_native_fixed_bracket_requires_paper_proof_not_runner_permissions(monkeypatch):
    import ab_v3_live as v3
    monkeypatch.setenv('BUILDER_ORDER_PROVIDER','crosstrade')
    monkeypatch.setenv('V3_RUNNER_ENABLED','false');monkeypatch.setenv('V3_EXIT_MODE','shadow')
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026')
    assert v3.native_fixed_bracket()
    p={};monkeypatch.setattr(gate,'policy',lambda:p)
    assert v3.native_bracket_readiness()=='native_bracket_paper_verification_missing'
    p['broker_workflow_verification']=dict(verified=True,simulator_only=True,
        provider='CrossTrade',surface='tradovate',contract='MNQZ2026',paper_evidence_reference='TEST_ONLY',
        native_oso_oco=True,partial_fill_repair=True,restart_recovery=True,net_accounting=True)
    assert v3.native_bracket_readiness()=='native_bracket_paper_verification_missing'
    p['broker_workflow_verification']['simulator_only']=False
    assert v3.native_bracket_readiness() is None
    monkeypatch.setenv('V3_RUNNER_ENABLED','true')
    assert not v3.native_fixed_bracket()


def test_fixed_native_prepared_path_does_not_arm_advanced_manager(sim,monkeypatch):
    import ab_v3_live as v3
    f=sim
    monkeypatch.setattr(v3,'mode',lambda:'LIVE')
    monkeypatch.setattr(v3,'native_fixed_bracket',lambda:True)
    monkeypatch.setattr(v3,'native_bracket_readiness',lambda:None)
    monkeypatch.setattr(v3,'setting_blockers',lambda:[])
    monkeypatch.setattr(v3,'exit_blockers',Mock(side_effect=AssertionError('advanced manager called')))
    monkeypatch.setattr(v3,'frozen_parity_ok',Mock(side_effect=AssertionError('advanced parity called')))
    monkeypatch.setattr(v3,'connect',f.api.connect);monkeypatch.setattr(v3,'contract',f.api.contract)
    monkeypatch.setattr(v3,'route',f.api.route);monkeypatch.setattr(v3,'now_ms',f.api.now_ms)
    monkeypatch.setattr(v3,'m1_fresh',lambda *args:True)
    monkeypatch.setattr(v3.builder_broker_sync,'entry_blocker',lambda **kwargs:None)
    monkeypatch.setattr(v3.directional_ai_review,'check',lambda *args:(None,{'state':'ALLOW'}))
    s=dict(_v3_directional=True,_continuation_order_id='order-one',dir='LONG',entry=30000.,SL=29990.,TP=30030.)
    assert v3.prepared_entry_blocker(s) is None
    monkeypatch.setattr(v3,'native_bracket_readiness',lambda:'native_bracket_paper_verification_missing')
    assert v3.prepared_entry_blocker(s)=='v3:native_bracket_paper_verification_missing'
