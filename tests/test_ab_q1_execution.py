"""Q1 routing acceptance: synthetic market/AI and injected broker only."""
import copy
import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest

import ab_q1_execution as q
import ab_q1_review as review
import ab_ai_context
import execution_validation as gate
import execution_policy
import directional_risk_revalidation as risk
import directional_pending
import test_builder_crosstrade as protocol
from test_ab_q1_review import signal
from test_directional_hybrid import h
from test_directional_setup_review import service
from test_directional_resting_policy import policy
from test_directional_risk_revalidation import native, guarded

@pytest.fixture(autouse=True)
def isolated_process_state():
    # The legacy unittest broker fixture uses patch.stopall(); restore the
    # enclosing process after its nested fixtures, including their env snapshots.
    environment=dict(os.environ);service_before=ab_ai_context.INSTANCE;sources=dict(gate._SOURCES)
    try:yield
    finally:
        os.environ.clear();os.environ.update(environment)
        ab_ai_context.INSTANCE=service_before
        gate._SOURCES.clear();gate._SOURCES.update(sources)


REAL_CHECK=gate.check


def setup(now,side='LONG',key='q1'):
    s=signal(side);s['bos_ms']=int(now*1000)//60000*60000-60000
    return review.candidate(s,key,dict(AB_V3_CONTRACT='MNQZ2026',EXEC_CANCEL_AFTER_SEC='600'))


def flags(monkeypatch):
    for k,v in dict(EXEC_STRATEGY_POLICY=q.MODE,AB_Q1_ENTRY_ARMED='1',AB_Q1_AI_REVIEW_ENABLED='true',
        AB_Q1_AI_SESSIONS='ASIA,PREM,NYAM,NYPM',AB_V3_TV_MAPPING_VERIFIED='1',
        AB_MANAGER_MODE='shadow',V3_EXIT_REAL_EXECUTION='false',V3_RUNNER_ENABLED='false',
        AUTO_SUBMIT='1',EXEC_MAX_QTY='40').items():monkeypatch.setenv(k,v)


def qpolicy(binding):
    p=policy();p.update(strategy='A/B',quality='Q1',target_rule='ORIGINAL_AB_TARGET',fixed_gross_rr=None,
        max_quantity=40,account_binding=binding)
    return p


@pytest.fixture
def routed(h,tmp_path,monkeypatch):
    f=protocol.NativeTests();f.setUp()
    flags(monkeypatch)
    f.now=h.now
    # The exact real shared review implementation; its transport is mocked.
    h.support.config.update(AB_Q1_AI_REVIEW_ENABLED='true',AB_Q1_AI_SESSIONS='ASIA,PREM,NYAM,NYPM')
    monkeypatch.setattr(q.time,'time',lambda:f.now)
    # Default function clock is bound at definition, so inject the actual
    # permission with an explicit synthetic clock, not a fake ALLOW gate.
    real_permission=q.permission
    monkeypatch.setattr(q,'permission',lambda o,**kw:real_permission(o,clock=lambda:f.now))
    monkeypatch.setattr(q.live,'connect',f.api.connect)
    monkeypatch.setattr(q.live,'route',f.api.route)
    monkeypatch.setattr(q.live,'now_ms',f.api.now_ms)
    monkeypatch.setattr(q.dispatches,'DB_PATH',tmp_path/'dispatch.db');q.dispatches._init_db()
    monkeypatch.setattr(ab_ai_context,'INSTANCE',h.support)
    order=setup(f.now);f.order=dict(order,quantity=2,route_id=f.api.route(),account_label='Builder 50K',
        lifecycle=q.LIFECYCLE,runner_enabled=False,risk_points=10,baseline_target_price=order['target_price'])
    with f.api.connect() as db:db.execute('DELETE FROM orders')
    f.prepare()
    p=qpolicy(f.sync.binding);path=tmp_path/'q1-policy.json';path.write_text(json.dumps(p))
    monkeypatch.setenv('AB_Q1_EXECUTION_POLICY_FILE',str(path))
    f.qpolicy=p;f.qpath=path;f.review=h.support;f.order_base=order
    try:yield f,h
    finally:
        monkeypatch.undo()
        f.doCleanups()


def allow(f,h,side='LONG'):
    if side=='SHORT':
        o=setup(f.now,side);f.order.update(o,baseline_target_price=o['target_price']);f.prepare()
    assert f.review.queue_setup(f.order)['state']=='queued'
    assert f.review.process_one()
    assert review.check(f.order,f.review)[0] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_tick_limit_never_worsened_and_review_freezes_all_levels(side):
    s=signal(side);s.update(entry=100.13,SL=90.13 if side=='LONG' else 110.13,TP=130.13 if side=='LONG' else 70.13)
    import ab_quality
    ab_quality.attach(s)
    o=review.candidate(s,'ticks',dict(AB_V3_CONTRACT='MNQZ2026'))
    sign=1 if side=='LONG' else -1
    assert sign*(o['entry_price']-s['entry'])<=0
    assert sign*(o['stop_price']-s['SL'])<=0
    assert sign*(o['target_price']-s['TP'])<=0
    assert review.describe(o,'MNQZ2026')['entry_price']==o['entry_price']
    assert o['q1_source']['original_levels']=={k:s[k] for k in ('entry','SL','TP')}


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_q1_exact_ai_to_native_ack_fill_protection_close(routed,monkeypatch,side):
    f,h=routed;allow(f,h,side)
    # Real final execution validator with explicitly synthetic source proofs.
    now=int(f.now*1000);end=now//60000*60000
    candle=dict(source='AUTHENTICATED_TV_CLOSED_M1',order_id=f.order['order_id'],contract='MNQZ2026',closed=True,
        feed_authenticated=True,market_open=True,evidence_id='SYNTHETIC_M1',bar_open_ms=end-60000,bar_close_ms=end,
        received_ms=end,checked_ms=now,ohlc=dict(open=101.,high=105.,low=95.,close=102.))
    monkeypatch.setattr(gate,'_SOURCES',dict(market=lambda o:candle,
        risk=lambda o:dict(order_id=o['order_id'],allowed=True,account_binding=f.sync.binding,checked_ms=now),
        thesis=lambda o:dict(order_id=o['order_id'],valid=True,evidence_id='SYNTHETIC_STRUCTURE',checked_ms=now)))
    monkeypatch.setattr(gate,'check',lambda o,r:REAL_CHECK(o,r,clock=lambda:f.now))
    result=f.submit();assert result['sent']
    assert result.get('filled',False) is False
    assert f.posts[0][1]['clOrdId']==f.posts[0][1]['orderId']
    assert f.posts[0][1]['limitPrice']==f.order['entry_price']
    assert f.posts[0][1]['takeProfit']==f.order['target_price']
    assert f.posts[0][1]['tif']=='gtd'
    with f.api.connect() as db:
        assert db.execute('SELECT position_json FROM orders WHERE order_id=?',(f.order['order_id'],)).fetchone()[0] is None
    f.open();assert f.t.row(f.order['order_id'])['state']=='OPEN'
    obs=json.loads(f.t.row(f.order['order_id'])['observation_json'])
    assert obs['protection_verified'] is True
    snap,net=f.complete();f.t.reconcile_one(f.t.row(f.order['order_id']),snap)
    f.t.reconcile_costs(snap)
    assert f.t.row(f.order['order_id'])['state']=='CLOSED'
    assert len(f.posts)==1


@pytest.mark.parametrize('fault',['WAIT','missing','changed','expired','wrong_policy','unarmed','kill','raw_ab'])
def test_no_post_for_invalid_q1_or_permissions(routed,monkeypatch,fault):
    f,h=routed
    if fault!='missing':allow(f,h)
    if fault=='WAIT':monkeypatch.setattr(f.review,'setup_annotation',lambda *a:dict(state='WAIT',owner='A/B'))
    if fault=='changed':f.order['target_price']+=1;f.prepare()
    if fault=='expired':f.now+=601
    if fault=='wrong_policy':monkeypatch.setenv('EXEC_STRATEGY_POLICY','AB_ONLY')
    if fault=='unarmed':monkeypatch.setenv('AB_Q1_ENTRY_ARMED','0')
    if fault=='kill':monkeypatch.setenv('EXEC_KILL_SWITCH','true')
    if fault=='raw_ab':f.order.pop('q1_quality');f.prepare()
    with pytest.raises(q.broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert f.posts==[]


def test_q1_final_gate_is_never_legacy_ab_exemption(routed):
    f,h=routed;allow(f,h);f.final_gate.return_value='account_risk_not_revalidated'
    result=f.t.submit(f.payload(),f.order['order_id'])
    assert not result['sent'] and result['reason']=='account_risk_not_revalidated'
    f.final_gate.assert_called_once();assert f.posts==[]


def test_q1_restart_unknown_is_not_retried(routed):
    f,h=routed;allow(f,h);f.timeout=True
    result=f.submit();assert result['submission_unknown']
    restarted=q.broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    with pytest.raises(q.broker.TransportError):restarted.submit(f.payload(),f.order['order_id'])
    assert len(f.posts)==1


def test_q1_permission_rechecked_after_delayed_validation(routed,monkeypatch):
    f,h=routed;allow(f,h)
    f.final_gate.side_effect=lambda *a:monkeypatch.setenv('AB_Q1_ENTRY_ARMED','0')
    result=f.t.submit(f.payload(),f.order['order_id'])
    assert not result['sent'] and result['reason']=='q1_entry_not_armed' and not f.posts


def test_q1_uses_independent_policy_and_preserves_swing_target(routed,monkeypatch):
    f,h=routed
    p=gate.policy_for(f.order);assert p['target_rule']=='ORIGINAL_AB_TARGET' and p['fixed_gross_rr'] is None
    assert gate.reward_risk(f.order,p)['gross_rr']==3
    p['approved']=False;f.qpath.write_text(json.dumps(p))
    with pytest.raises(ValueError,match='validated_execution_policy_missing'):gate.policy_for(f.order)
    monkeypatch.delenv('AB_Q1_EXECUTION_POLICY_FILE')
    monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(f.qpath))
    with pytest.raises(ValueError):gate.policy_for(f.order)


def test_q1_coordinator_wait_once_then_dispatch_once_and_no_recovery(routed):
    f,h=routed;order=f.order_base
    assert q.dispatches._claim(order,'test')
    q.dispatches._finish(order['order_id'],dict(state='WAITING_AI'),'test')
    f.review.queue_setup(order)
    dispatch=Mock(return_value=dict(state='SENT',reason='mock_ack'))
    q.resume(dispatch,f.review,clock=lambda:f.now);dispatch.assert_not_called()
    f.review.process_one();q.resume(dispatch,f.review,clock=lambda:f.now)
    q.resume(dispatch,f.review,clock=lambda:f.now)
    dispatch.assert_called_once();assert dispatch.call_args.args[0]['nofill_recovery_allowed'] is False
    f.now+=601;q.resume(dispatch,f.review,clock=lambda:f.now);assert dispatch.call_count==1


def test_q1_coordinator_expiry_is_terminal(routed):
    f,h=routed;order=f.order_base;q.dispatches._claim(order,'test')
    q.dispatches._finish(order['order_id'],dict(state='WAITING_AI'),'test')
    f.review.queue_setup(order);f.now+=601
    dispatch=Mock();q.resume(dispatch,f.review,clock=lambda:f.now);dispatch.assert_not_called()
    row=q.dispatches.rows()[0];assert row['state']=='BLOCKED'


def test_legacy_ab_signal_cannot_use_shared_policy(routed):
    f,h=routed
    raw=dict(_strat='A/B',dir='LONG',entry=100,SL=90,TP=130)
    assert execution_policy.entry_blocker(raw)=='q1_prepared_signal_required'


def test_q1_uses_real_account_risk_validator(q_native,monkeypatch,tmp_path):
    f=q_native;flags(monkeypatch)
    # Use an eligible NYAM instant, keeping all fixture account times current.
    o=setup(f.now);f.order=dict(o,quantity=2,route_id=f.api.route(),account_label='Builder 50K')
    with f.api.connect() as db:db.execute('DELETE FROM orders')
    f.prepare()
    with q.dispatches._connect() as db:db.execute('DELETE FROM live_dispatches')
    assert q.dispatches._claim(o,'test')
    monkeypatch.setattr(q,'permission',lambda o,**kw:None)  # independent permission tests above
    monkeypatch.setattr(q.live,'native_bracket_readiness',lambda *a:None)
    monkeypatch.setattr(gate,'policy_for',lambda o:dict(contract='MNQZ2026',costs=dict(contract='MNQZ2026',validation_reference='MOCK',round_trip_usd=1.2,stop_slippage_ticks=0)))
    proof=f.validator(f.order);assert proof['allowed'] is True
    assert f.guard_check.call_args.kwargs['reservation_id']=='ab_q1_'+o['order_id']
    f.values['/accounts/Builder%20account/positions']=[dict(accountId=2214,contractId=100,netPos=1)]
    assert f.validator(f.order)['reason']=='broker:account_has_positions_or_orders'
    assert not f.posts


def test_full_q1_coordinator_dispatch_sizing_native_and_duplicate_barrier(routed,monkeypatch):
    f,h=routed;allow(f,h)
    with f.api.connect() as db:db.execute('DELETE FROM orders')
    order=f.order_base;q.dispatches._claim(order,'test');q.dispatches._finish(order['order_id'],dict(state='WAITING_AI'),'test')
    monkeypatch.setattr(q.live,'native_bracket_readiness',lambda p:None)
    size=Mock(return_value=dict(allowed=True,max_quantity=3,risk_per_contract_usd=23.24))
    monkeypatch.setattr(risk,'size_for_entry',size)
    monkeypatch.setattr(q.guardrails,'account_profile',lambda:dict(config_ok=True))
    monkeypatch.setattr(q.guardrails,'exec_mode',lambda:'auto')
    guard=Mock(return_value=(True,'ok'));monkeypatch.setattr(q.guardrails,'guard_ok',guard)
    reserve=Mock(return_value=True);monkeypatch.setattr(q.guardrails,'begin_sibling_batch',reserve)
    for name in ('touch_sibling_batch','finish_sibling_batch','note'):monkeypatch.setattr(q.guardrails,name,Mock())
    def run(o):return q.dispatch(o,budget_builder=lambda p:243.60,feed_age=lambda:0,market_open=lambda:True,
                                news_flags=lambda s:([],False),calendar_age=lambda:0)
    q.resume(run,f.review,clock=lambda:f.now)
    assert len(f.posts)==1 and f.posts[0][1]['qty']==3
    assert size.call_args.args[0]['SL']==90 and size.call_args.args[0]['_risk_budget_usd']==243.60
    assert reserve.call_args.args==('ab_q1_'+order['order_id'],243.60,['A/B'])
    assert q.dispatches.rows()[0]['state']=='SENT'
    q.resume(run,f.review,clock=lambda:f.now);assert len(f.posts)==1
    assert q.dispatch(order,budget_builder=lambda p:243.60,feed_age=lambda:0,market_open=lambda:True,
                      news_flags=lambda s:([],False),calendar_age=lambda:0)['reason']=='q1_already_prepared_no_retry'
    assert len(f.posts)==1


@pytest.mark.parametrize('fault',['valid','missing','touched','changed','opposing'])
def test_q1_structural_thesis_blocks_missing_or_contradictory_evidence(routed,monkeypatch,fault):
    f,h=routed
    original_reader=f.review.reader
    def bounded_source():
        data=original_reader();packet=data['context']['packet']
        for evidence in packet['evidence'].values():evidence.update(low=95.,high=105.)
        packet['packet_id']=ab_ai_context.digest({k:v for k,v in packet.items() if k!='packet_id'})
        return data
    f.review.reader=bounded_source
    original_transport=f.review.transport
    def bounded_response(payload):
        raw=original_transport(payload);decision=json.loads(raw['output'][0]['content'][0]['text'])
        for side in ('long_scenario','short_scenario'):decision[side]['invalidation']['price']=95.
        raw['output'][0]['content'][0]['text']=json.dumps(decision)
        return raw
    f.review.transport=bounded_response
    allow(f,h);o=f.order
    bos=o['q1_source']['bos_ms']
    bar=dict(ts_event=datetime.fromtimestamp(bos/1000,timezone.utc).isoformat(),open=100.,high=105.,low=95.,close=102.)
    o['q1_bos_bar']=bar
    q.dispatches._claim(o,'test')
    with f.api.connect() as db:
        db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?)',('M1','MNQZ2026',bos,json.dumps(bar),bos+60000))
    if fault=='changed':
        with f.api.connect() as db:db.execute('UPDATE bars SET bar_json=?',(json.dumps(dict(bar,high=106)),))
    else:
        f.now+=60;h.now=f.now
        if fault!='missing':
            b=dict(bar,low=85 if fault=='touched' else 95,ts_event=datetime.fromtimestamp((bos+60000)/1000,timezone.utc).isoformat())
            with f.api.connect() as db:db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1','MNQZ2026',bos+60000,json.dumps(b),bos+120000))
        if fault=='opposing':
            with f.review.connect() as db:
                db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',('opposing',f.now,json.dumps(dict(symbol='MNQ',kind='MSS',direction='DOWN'))))
    result=q.thesis(o,lambda:f.now)
    if fault=='valid':assert result['valid'],result
    else:assert not result['valid'] and result['reason'] != 'q1_thesis_evidence_unavailable'
    assert not f.posts


def test_pending_q1_preserves_shared_account_checks(q_native,monkeypatch):
    f=q_native;flags(monkeypatch)
    o=setup(f.now);f.order=dict(o,quantity=2,route_id=f.api.route(),account_label='Builder 50K',
                               ai_review_at_native_dispatch=dict(review_id='mock',expires_at=f.now+120))
    f.prepare();q.dispatches._claim(o,'test')
    monkeypatch.setattr(q,'permission',lambda o:None)
    monkeypatch.setattr(review,'check',lambda *a:(None,dict(review_id='mock',expires_at=f.now+120)))
    monkeypatch.setattr(q.live,'m1_fresh',lambda *a:True)
    monkeypatch.setattr(directional_pending,'ThesisValidator',lambda **kw:lambda *a,**kw:dict(valid=True))
    monkeypatch.setattr(gate,'policy_for',lambda o:dict(contract='MNQZ2026',costs=dict(contract='MNQZ2026',validation_reference='MOCK',round_trip_usd=1.2,stop_slippage_ticks=0)))
    v=directional_pending.PendingValidator(signal_builder=Mock(side_effect=AssertionError('NOT_DIRECTIONAL')),
        budget_builder=lambda p:250,feed_age=lambda:0,market_open=lambda:True,news_flags=lambda x:([],False),calendar_age=lambda:0,clock=lambda:f.now)
    assert v(f.order,f.snapshot(orders=[dict(id=11)]),{11}) is None
    assert v(f.order,f.snapshot(orders=[dict(id=11),dict(id=99)]),{11})=='pending_foreign_order'
    assert v(f.order,f.snapshot(position=1),{11})=='pending_account_exposure'
    assert not f.posts


def test_q1_expiry_cancels_parent_then_reconciles_protected_late_fill(routed):
    f,h=routed;allow(f,h);f.submit()
    f.now+=61;f.t.reconcile(f.snapshot())
    assert any(path.endswith('/11/cancel') for path,body in f.posts)
    f.now+=600
    for oid in (11,12,13):f.life[oid]['order']['ordStatus']='Filled' if oid==11 else 'Working'
    fill=f.fill(101,11,2,f.order['entry_price'],'Buy')
    f.t.reconcile(f.snapshot([fill],2))
    obs=json.loads(f.t.row(f.order['order_id'])['observation_json'])
    assert obs['setup_expired_fill'] and obs['protection_verified']
    assert sum(path.endswith('/orders/place') for path,body in f.posts)==1
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)


def test_scanner_observation_claim_is_idempotent_and_not_history_replayed(routed):
    f,h=routed;s=signal();s['bos_ms']=int(f.now)//60*60000-60000
    bos=s['bos_ms'];bar=dict(ts_event=datetime.fromtimestamp(bos/1000,timezone.utc).isoformat(),open=100,high=105,low=95,close=101)
    with f.api.connect() as db:db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1','MNQZ2026',bos,json.dumps(bar),bos+60000))
    q.observe(s,'fresh',f.review,clock=lambda:f.now)
    q.observe(s,'fresh',f.review,clock=lambda:f.now)
    rows=q.dispatches.rows();assert len(rows)==1 and rows[0]['state']=='WAITING_AI'
    f.now+=601;q.observe(s,'old',f.review,clock=lambda:f.now)
    assert len(q.dispatches.rows())==1 and not f.posts


@pytest.fixture
def q_native(native,monkeypatch):
    try:yield native
    finally:monkeypatch.undo()


def test_shared_route_keeps_directional_permission_and_blocks_second_account_order(routed,monkeypatch):
    f,h=routed;allow(f,h);f.submit()
    directional=dict(order_id='directional-second',candidate_id='second',strategy='AB_DIRECTIONAL',direction='LONG',
        activation_ms=int(f.now*1000),expiry_ms=int(f.now*1000)+600000,entry_price=100.,stop_price=90.,target_price=120.,
        quantity=2,contract='MNQZ2026',route_id=f.api.route(),account_label='Builder 50K')
    assert f.t.directional_permission(directional) is None
    f.order=directional;f.prepare()
    import directional_ai_review
    monkeypatch.setattr(directional_ai_review,'check',lambda *a:(None,None))
    with pytest.raises(q.broker.TransportError,match='FRESH_FLAT_ACCOUNT_REQUIRED'):
        f.t.submit(f.payload(),directional['order_id'])
    assert len(f.posts)==1


def test_q1_shared_scope_does_not_approve_existing_directional_proof(routed,monkeypatch):
    f,h=routed
    p=copy.deepcopy(f.qpolicy)
    p['broker_workflow_verification']=dict(verified=True,simulator_only=False,provider='CrossTrade',surface='tradovate')
    assert q.live.native_bracket_readiness(p)=='native_bracket_shared_strategy_scope_unverified'


def test_q1_session_allowance_preserves_guard_loss_limit(guarded,monkeypatch):
    state,stats,original,check,latch=guarded
    monkeypatch.setenv('SKIP_SESSIONS','NYAM')
    assert check()==(False,'session:NYAM')
    o=setup(1792504800.)
    x=q.signal(o);x['_planned_group_risk_usd']=250.
    state['pending_group'].update(group_id='ab_q1_'+o['order_id'],strats=['A/B'])
    result=q.guardrails.guard_ok(x,0,True,False,0,reservation_id='ab_q1_'+o['order_id'])
    assert result==(True,'ok')
    stats['losses']=1
    assert q.guardrails.guard_ok(x,0,True,False,0,reservation_id='ab_q1_'+o['order_id'])==(False,'day_loss_n')
    assert os.environ['SKIP_SESSIONS']=='NYAM'
