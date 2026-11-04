"""ICT automatic route: MOCK AI + injected in-memory broker, never real orders."""
import copy,json,os
from pathlib import Path
from unittest.mock import Mock
import pytest
import ict_execution as ict
import ict_shadow_research as research
import execution_validation as gate
import continuation_live as journal
import ab_v3_live as live
import builder_crosstrade as broker
from test_ict_order_handoff import record as old_record,T,current
from test_directional_resting_policy import policy,case
import test_builder_crosstrade as protocol


def record(short=False):
    r=old_record(short);r.update(id='ICT_INDEPENDENT_NYAM_20261030_99',requested_at=T+1)
    p=r['packet'];p.pop('packet_id');p['execution_contract']=ict.VERSION;p['packet_id']=research.digest(p)
    r['decision']['packet_id']=p['packet_id'];r['decision']['plan']['invalidation_mode']=ict.VERSION
    r['response']['output'][0]['content'][0]['text']=json.dumps(r['decision'])
    r['validation']=research.validate(p,r['decision'],r['received_at'])
    return r


def save(r,root):
    path=Path(root)/'research/ict_2022_nyam_20261030'/(r['id']+'.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(r))


def ipolicy(binding='simulator'):
    p=policy();p.update(strategy='ICT_AI',target_rule='FIXED_2R_GROSS',risk_percent=.5,
        thesis_rule_version=ict.VERSION,max_quantity=40,account_binding=binding)
    return p


@pytest.fixture
def routed(tmp_path,monkeypatch):
    import ict_news_context
    monkeypatch.setattr(ict_news_context,'build',lambda config,now:dict(status='CLEAR',next_blackout_at=None))
    env=dict(os.environ);f=protocol.NativeTests();f.setUp();f.now=T+11
    flags=dict(ICT_EXECUTION_ENABLED='true',ICT_EXECUTION_ARMED_AFTER_MS=str(int(T*1000)),
        ICT_SHADOW_ENABLED='true',AB_V3_TV_MAPPING_VERIFIED='1',EXEC_MAX_QTY='40',AUTO_SUBMIT='1',
        AB_MANAGER_MODE='shadow',V3_EXIT_MODE='shadow',V3_EXIT_REAL_EXECUTION='false',V3_RUNNER_ENABLED='false')
    for k,v in flags.items():monkeypatch.setenv(k,v)
    r=record();save(r,f.tmp.name)
    o=ict.from_record(r,f.now);f.order=dict(o,quantity=2,route_id=f.api.route(),account_label='Builder 50K',lifecycle='ICT_NATIVE_FIXED_BRACKET_V1',risk_points=5.25)
    with f.api.connect() as db:db.execute('DELETE FROM orders')
    f.prepare();f.r=r;f.base=o
    path=tmp_path/'ict.json';path.write_text(json.dumps(ipolicy(f.sync.binding)));monkeypatch.setenv('ICT_EXECUTION_POLICY_FILE',str(path))
    monkeypatch.setattr(live,'connect',f.api.connect);monkeypatch.setattr(live,'route',f.api.route);monkeypatch.setattr(live,'now_ms',f.api.now_ms)
    monkeypatch.setattr(journal,'DB_PATH',tmp_path/'dispatch.db');journal._init_db()
    # real saved-response verifier, explicit fake clock; no fake approval.
    real=ict.review_check;monkeypatch.setattr(ict,'review_check',lambda o,**kw:real(o,clock=lambda:f.now))
    try:yield f
    finally:
        monkeypatch.undo();f.doCleanups();os.environ.clear();os.environ.update(env)


@pytest.mark.parametrize('short',[False,True])
def test_structural_geometry_and_fresh_exact_record(tmp_path,monkeypatch,short):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');monkeypatch.setenv('DATA_DIR',str(tmp_path))
    r=record(short);save(r,tmp_path);o=ict.from_record(r,T+11)
    reason,a=ict.review_check(o,clock=lambda:T+11)
    assert reason is None and a['state']=='ALLOW'
    assert o['target_price']==o['entry_price']+(1 if not short else -1)*2*abs(o['entry_price']-o['stop_price'])
    assert o['stop_price']==r['decision']['plan']['stop_price']
    for k in ('entry_price','stop_price','target_price','activation_ms','expiry_ms'):
        changed=dict(o);changed[k]+=.25
        assert ict.review_check(changed,clock=lambda:T+11)[0]
    assert ict.review_check(o,clock=lambda:T+601)[0]
    r['state']='research_unknown';save(r,tmp_path);assert ict.review_check(o,clock=lambda:T+11)[0]


@pytest.mark.parametrize('decision',['WAIT','NO_TRADE'])
def test_no_order_from_nonproposal(monkeypatch,decision):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');r=record();r['decision'].update(decision=decision,plan=None)
    with pytest.raises(ValueError):ict.from_record(r,T+11)


def test_legacy_or_discretionary_invalidation_not_executable(monkeypatch):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026')
    with pytest.raises(ValueError,match='invalidation'):ict.from_record(old_record(),T+11)
    r=record();r['decision']['plan']['invalidation_mode']='UNSUPPORTED_ADDITIONAL_CONDITION'
    r['response']['output'][0]['content'][0]['text']=json.dumps(r['decision'])
    with pytest.raises(ValueError,match='invalidation'):ict.from_record(r,T+11)


@pytest.mark.parametrize('key,value,reason',[
 ('ICT_EXECUTION_ENABLED','false','ict_execution_disabled'),
 ('ICT_EXECUTION_ARMED_AFTER_MS','0','ict_forward_activation_epoch_required'),
 ('ICT_EXECUTION_ARMED_AFTER_MS',str(int((T+2)*1000)),'ict_forward_activation_epoch_required'),
 ('EXEC_KILL_SWITCH','true',None),('LIVE_EXECUTION_ENABLED','false',None),
 ('EXEC_STRATEGY_POLICY','SHADOW_ALL','ict_shared_route_disabled'),
 ('V3_EXIT_REAL_EXECUTION','true','ict_fixed_broker_bracket_only'),
 ('AB_V3_TV_MAPPING_VERIFIED','0','ict_contract_unverified')])
def test_permissions_are_separate_and_forward_only(routed,monkeypatch,key,value,reason):
    f=routed;monkeypatch.setenv(key,value)
    why=ict.permission(f.order,clock=lambda:f.now)
    assert why and (reason is None or why==reason)
    with pytest.raises(broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert not f.posts


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_native_ack_fill_oco_close_accounting(routed,side):
    f=routed
    if side=='SHORT':
        r=record(True);save(r,f.tmp.name);o=ict.from_record(r,f.now);f.order.update(o);f.r=r;f.prepare()
    result=f.submit();assert result['sent']
    assert f.local()['position_json'] is None
    body=f.posts[-1][1];assert body['clOrdId']==body['orderId']
    assert body['stopLoss']==f.order['stop_price'] and body['takeProfit']==f.order['target_price']
    assert body['tif']=='gtd' and body['requireMarketPosition']=='flat'
    f.open();assert f.local()['state']=='OPEN'
    assert f.t.row(f.order['order_id'])['state']=='OPEN'
    f.fixture_round_trip=ipolicy()['costs']['round_trip_usd']
    snap,net=f.complete();f.t.reconcile_one(f.t.row(f.order['order_id']),snap)
    assert f.t.row(f.order['order_id'])['state']=='CLOSED_PENDING_COSTS'
    assert f.t.entry_blocker()
    f.t.reconcile_costs(snap)
    assert f.t.row(f.order['order_id'])['state']=='CLOSED'
    assert f.t.resolution(f.order['order_id'])['net']==pytest.approx(net)


def test_timeout_and_restart_cannot_duplicate(routed):
    f=routed;f.timeout=True;result=f.t.submit(f.payload(),f.order['order_id'])
    assert result['submission_unknown'] and len(f.posts)==1
    f.t=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    with pytest.raises(broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert len(f.posts)==1


def test_rejected_ack_not_fill_and_blocks_exposure(routed):
    f=routed;f.bad_ack=True;r=f.t.submit(f.payload(),f.order['order_id'])
    assert not r['sent'] and r['submission_unknown'];assert f.local()['position_json'] is None
    assert f.t.entry_blocker()


def test_fixed_rr_policy_keeps_all_gate_evidence():
    a=case();o=a[0];o.update(strategy='ICT_AI',order_id='ICT-'+'a'*40,ict_fingerprint='a'*64,
        ict_review_id='ICT_INDEPENDENT_NYAM_20261030_99',ict_thesis_version=ict.VERSION,order_type='LIMIT',runner_enabled=False)
    for index in (1,2,3,4):a[index]['order_id']=o['order_id']
    a[4]['setup']=dict(o);a[5]=ipolicy()
    assert gate.evaluate(*a) is None
    for index,key,val in [(1,'feed_authenticated',False),(2,'valid',False),(3,'allowed',False),(4,'state','WAIT')]:
        changed=copy.deepcopy(a);changed[index][key]=val;assert gate.evaluate(*changed)
    a[5]['strategy']='AB_DIRECTIONAL';assert gate.evaluate(*a)


@pytest.mark.parametrize('direction,eventdirection,blocked',[('LONG','UP',False),('LONG','DOWN',True),('SHORT','DOWN',False),('SHORT','UP',True)])
def test_only_opposing_structure_invalidates(monkeypatch,direction,eventdirection,blocked):
    import directional_hybrid_context
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');r=record(direction=='SHORT');o=ict.from_record(r,T+11)
    p=current(T+60);p['evidence']=copy.deepcopy(r['packet']['evidence'])
    for symbol in ('MNQ','NQ'):
        p['evidence'][symbol]=dict(kind='bar',symbol=symbol,timeframe=1,time=T,available_at=T+60,low=113 if direction=='SHORT' else 104,high=116 if direction=='SHORT' else 107)
    monkeypatch.setattr(directional_hybrid_context,'build',lambda p:{'structure_events':{'value':[dict(symbol='MNQ',kind='BOS',direction=eventdirection,available_at=T+60)]}})
    assert bool(ict.market_check(o,r,p,T+70))==blocked
    del p['evidence']['NQ'];assert ict.market_check(o,r,p,T+70)=='ict_post_review_context_gap'


def test_guard_owned_reservation_and_shared_account_collision(routed):
    import guardrails
    f=routed;x=ict.signal(f.base);x['_planned_group_risk_usd']=243.60
    pg=dict(group_id='ict_ai_'+f.base['order_id'],planned_risk_usd=243.60,strats=['ICT AI'],accepted=[])
    with __import__('unittest').mock.patch.object(guardrails,'_active_pending_group',return_value=pg):
        assert guardrails._reserved_entry_blocker(x,pg['group_id'],{}) is None
        assert guardrails._reserved_entry_blocker(x,None,{})=='group_pending'
        assert guardrails._reserved_entry_blocker(x,'ab_directional_'+f.base['order_id'],{})


def test_process_claim_survives_restart_no_replay(routed,monkeypatch):
    f=routed;monkeypatch.setattr(ict.time,'time',lambda:f.now)
    real=ict.permission;monkeypatch.setattr(ict,'permission',lambda o:real(o,clock=lambda:f.now))
    monkeypatch.setattr(ict,'_CALLBACKS',{})
    dispatch=Mock(return_value=dict(state='SUBMISSION_UNKNOWN',reason='MOCK_TIMEOUT'))
    monkeypatch.setattr(ict,'dispatch',dispatch)
    assert ict.process(f.r)['state']=='SUBMISSION_UNKNOWN'
    assert ict.process(f.r)['state']=='SUBMISSION_UNKNOWN'
    assert dispatch.call_count==1
    # Second AI review of identical structural FVG cannot submit again.
    other=copy.deepcopy(f.r);other['id']='ICT_INDEPENDENT_NYAM_20261030_100';save(other,f.tmp.name)
    assert ict.process(other)['state']=='SUBMISSION_UNKNOWN' and dispatch.call_count==1


def test_budget_math_and_no_extra_ai_call():
    assert ict.equity_budget({'account':{'balance':{'netLiq':48721.05}}})==243.60
    for equity in (None,float('nan'),-1,True):
        with pytest.raises(ValueError):ict.equity_budget({'account':{'balance':{'netLiq':equity}}})


def test_pending_expiry_late_fill_preserves_working_children(routed):
    f=routed;f.submit();f.now+=61
    f.t.reconcile(f.snapshot())
    assert any(path.endswith('/11/cancel') for path,body in f.posts)
    f.open() # Fill after cancel request, with genuine mocked broker evidence.
    assert f.local()['state']=='OPEN'
    assert f.t.entry_blocker() # live position prevents additional exposure
    posts_before=len(f.posts)
    f.t=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    f.t.reconcile(f.sync.snapshot())
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts[posts_before:])


def test_partial_fill_keeps_exact_quantity_protection(routed):
    f=routed;f.submit();f.open(qty=1)
    row=f.t.row(f.order['order_id']);obs=json.loads(row['observation_json'])
    assert obs['remaining_quantity']==1
    assert f.t.entry_blocker()
    assert not any(path.endswith('/12/cancel') or path.endswith('/13/cancel') for path,body in f.posts)


def test_dispatch_uses_shared_guard_size_and_native_route(routed,monkeypatch):
    import guardrails, directional_risk_revalidation as risk
    import builder_broker_sync
    from types import SimpleNamespace
    from test_live_risk_budget import production_budget
    f=routed
    with f.api.connect() as db:db.execute('DELETE FROM orders')
    monkeypatch.setattr(ict,'permission',lambda o,**kw:None) # Permission has independent negative tests above.
    monkeypatch.setattr(live,'native_bracket_readiness',lambda p:None) # Broker proof fixture, not live attestation.
    monkeypatch.setattr(guardrails,'account_profile',lambda:{'config_ok':True})
    monkeypatch.setattr(guardrails,'exec_mode',lambda:'auto')
    monkeypatch.setattr(guardrails,'guard_ok',lambda *a,**kw:(True,'ok'))
    monkeypatch.setattr(risk,'size_for_entry',lambda s:{'allowed':True,'max_quantity':2})
    batch=Mock(return_value=True);monkeypatch.setattr(guardrails,'begin_sibling_batch',batch)
    monkeypatch.setattr(guardrails,'touch_sibling_batch',lambda *a:None)
    monkeypatch.setattr(guardrails,'note',lambda *a:None)
    # f.final_gate isolates final validation; review is the real saved-response verifier.
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_MODE','EQUITY_PERCENT')
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT','0.5')
    monkeypatch.setattr(builder_broker_sync,'instance',lambda:SimpleNamespace(
        enabled=True,state=lambda:{'current':True},
        snapshot=lambda:{'account':{'balance':{'netLiq':48721.05}}}))
    result=ict.dispatch(f.base,budget_builder=production_budget(),feed_age=lambda:0,market_open=lambda:True,news_flags=lambda s:([],False),calendar_age=lambda:0)
    assert result['state']=='SENT' and result['quantity']==2 and len(f.posts)==1
    assert batch.call_args.args==('ict_ai_'+f.base['order_id'],243.60,['ICT AI'])
    assert f.local()['state']=='WAITING_BROKER_FILL' and f.local()['position_json'] is None


def test_thesis_requires_original_dispatch_and_fresh_observation(routed,monkeypatch):
    import directional_hybrid_context
    f=routed;assert journal._claim(f.base,'mock')
    monkeypatch.setattr(research,'remote_run',lambda *a:dict(packet=current()))
    monkeypatch.setattr(directional_hybrid_context,'build',lambda p:{'structure_events':{'value':[]}})
    result=ict.thesis(f.base,clock=lambda:f.now)
    assert result['valid'] and result['evidence_id']
    f.now=T+71
    assert not ict.thesis(f.base,clock=lambda:f.now)['valid']


def test_independent_scope_cannot_inherit_directional_broker_approval(routed):
    assert live.native_bracket_readiness(ipolicy())=='native_bracket_ict_scope_unverified'


def test_future_schema_requires_explicit_machine_invalidation():
    s=research.schema('mock',ict.VERSION)
    plan=s['properties']['plan']['anyOf'][1]
    assert 'invalidation_mode' in plan['required']
    assert 'UNSUPPORTED_ADDITIONAL_CONDITION' in plan['properties']['invalidation_mode']['enum']

from test_directional_risk_revalidation import native
from test_directional_pending import validator


def make_risk_ict(f,monkeypatch):
    original=dict(f.order)
    original.update(order_id='ICT-'+'a'*40,candidate_id='mock',strategy='ICT_AI',ict_fingerprint='a'*64,
        ict_review_id='ICT_INDEPENDENT_NYAM_20261030_99',ict_thesis_version=ict.VERSION,order_type='LIMIT',runner_enabled=False)
    f.order=original;f.prepare()
    with journal._connect() as db:db.execute('DELETE FROM live_dispatches')
    assert journal._claim(original,'mock')
    monkeypatch.setattr(ict,'permission',lambda *a,**kw:None)
    monkeypatch.setattr(gate,'policy_for',lambda o:ipolicy(f.sync.binding))
    monkeypatch.setattr(live,'native_bracket_readiness',lambda *a:None)
    return original


def test_ict_final_risk_uses_fresh_account_and_own_reservation(native,monkeypatch):
    f=native;make_risk_ict(f,monkeypatch)
    proof=f.validator(f.order)
    assert proof['allowed'],proof
    assert f.guard_check.call_args.kwargs['reservation_id']=='ict_ai_'+f.order['order_id']
    assert f.guard_check.call_args.args[0]['_planned_group_risk_usd']==244.80
    f.order['quantity']=20;f.prepare()
    assert not f.validator(f.order)['allowed']
    assert not f.posts


def test_ict_pending_reuses_risk_and_cancels_for_missing_review(validator,monkeypatch):
    import directional_pending
    f,v=validator;make_risk_ict(f,monkeypatch)
    approved={'review_id':'MOCK','expires_at':f.now+120}
    monkeypatch.setattr(ict,'review_check',lambda *a,**kw:(None,approved))
    snapshot=f.snapshot(orders=[dict(id=11),dict(id=12),dict(id=13)])
    assert v(f.order,snapshot,{11,12,13}) is None
    monkeypatch.setattr(ict,'review_check',lambda *a,**kw:('ict_review_missing',None))
    assert v(f.order,snapshot,{11,12,13})=='ict_review_missing'
    assert snapshot['orders'] and not f.posts


def test_target_hit_during_ai_latency_cannot_revive_setup(monkeypatch):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');r=record();r['received_at']=T+70
    r['validation']=research.validate(r['packet'],r['decision'],T+70)
    o=ict.from_record(r,T+71);p=current(T+60)
    p['evidence']['during_ai']=dict(kind='bar',symbol='MNQ',timeframe=1,time=T,available_at=T+60,low=104,high=o['target_price'])
    assert ict.market_check(o,r,p,T+71)=='BRACKET_TOUCHED_BEFORE_SUBMISSION'


def test_operator_draft_cannot_enable_execution(routed,monkeypatch):
    import ict_operator
    base=ipolicy();base['strategy']='AB_DIRECTIONAL';base['broker_workflow_verification']={'historical_native_oso_oco_verified':True,'execution_strategies':['AB_DIRECTIONAL'],'max_quantity':5}
    monkeypatch.setattr(gate,'policy',lambda:copy.deepcopy(base))
    monkeypatch.setattr(live,'native_bracket_readiness',lambda p:None)
    d=ict_operator.draft()
    assert d['approved'] is False and d['broker_workflow_verification']['current_ict_broker_run_verified'] is False
    assert d['broker_workflow_verification']['max_quantity']==5 and d['risk_percent']==.5
    assert not d['broker_workflow_verification']['ict_operator_acceptance_reference']
    assert base['strategy']=='AB_DIRECTIONAL' and base['broker_workflow_verification']['execution_strategies']==['AB_DIRECTIONAL']


@pytest.mark.parametrize('status',['UNKNOWN','BLACKOUT','POLICY_BLOCKED'])
def test_news_blocks_native_submission_and_pending_thesis(routed,monkeypatch,status):
    import ict_news_context
    f=routed
    monkeypatch.setattr(ict_news_context,'build',lambda config,now:dict(status=status))
    assert ict.permission(f.order,clock=lambda:f.now)=='ict_news_'+status.lower()
    with pytest.raises(broker.TransportError):f.submit()
    assert not f.posts
    proof=ict.thesis(f.base,clock=lambda:f.now,pending=True)
    assert not proof['valid'] and proof['reason']=='ict_news_'+status.lower()
