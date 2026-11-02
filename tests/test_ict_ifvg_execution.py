"""Mock AI and in-memory broker only. No network, paid AI or real orders."""
import copy,json,os
from pathlib import Path
from unittest.mock import Mock
import pytest
import ict_execution as ict
import ict_shadow_research as research
import ict_order_handoff as handoff
import execution_validation as gate
import continuation_live as journal
import builder_crosstrade as broker
from test_ict_ifvg import setup
from test_ict_execution import routed,save,ipolicy
from test_ict_shadow_research import T

def record(short=False):
    p,d=setup(short)
    p.pop('packet_id');p.update(contract_configured='MNQZ2026',execution_contract=ict.VERSION,ifvg_execution_contract=ict.IFVG_VERSION)
    p['packet_id']=research.digest(p)
    d['packet_id']=p['packet_id'];d['ifvg_review']['packet_id']=p['packet_id']
    d['ifvg_review']['plan']['invalidation_mode']=ict.IFVG_VERSION
    raw=dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])
    return dict(id='ICT_INDEPENDENT_NYAM_20261030_199',state='research_completed',requested_at=T+61,received_at=T+70,
        packet=p,decision=d,response=raw,validation=research.validate(p,d,T+70))

def scope():return dict(approved=True,contract_version=ict.IFVG_VERSION,operator_acceptance_reference='MOCK_OPERATOR_ACCEPTANCE',armed_after_ms=int(T*1000))

@pytest.mark.parametrize('short',[False,True])
def test_exact_companion_binding_and_fixed_target(tmp_path,monkeypatch,short):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');monkeypatch.setenv('DATA_DIR',str(tmp_path))
    r=record(short);save(r,tmp_path);o=ict.from_record(r,T+71,'IFVG')
    assert o['ict_family']=='IFVG' and ict.is_order(o)
    assert ict.review_check(o,clock=lambda:T+71)[0] is None
    assert o['stop_price']==r['decision']['ifvg_review']['plan']['stop_price']
    assert o['target_price']==o['entry_price']+(-1 if short else 1)*2*abs(o['entry_price']-o['stop_price'])
    assert o['ict_liquidity_target']==r['decision']['ifvg_review']['plan']['target_price']
    for k in ('entry_price','stop_price','target_price','activation_ms','expiry_ms'):
        q=dict(o);q[k]+=.25;assert ict.review_check(q,clock=lambda:T+71)[0]
    q=dict(o);q.pop('ict_family');q.pop('ict_family_contract');assert ict.review_check(q,clock=lambda:T+71)[0]
    assert ict.review_check(o,clock=lambda:T+601)[0]
    r['decision']['ifvg_review']['plan']['stop_price']-=.25;save(r,tmp_path)
    assert ict.review_check(o,clock=lambda:T+71)[0]

@pytest.mark.parametrize('change',['wait','no_trade','missing','legacy','unsupported','raw','validation','future'])
def test_no_unbound_or_legacy_companion_order(monkeypatch,change):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');r=record()
    if change=='wait':r['decision']['ifvg_review'].update(decision='WAIT',plan=None)
    if change=='no_trade':r['decision']['ifvg_review'].update(decision='NO_TRADE',plan=None)
    if change=='missing':r['decision'].pop('ifvg_review')
    if change=='legacy':r['packet'].pop('ifvg_execution_contract')
    if change=='unsupported':r['decision']['ifvg_review']['plan']['invalidation_mode']='UNSUPPORTED_ADDITIONAL_CONDITION'
    if change=='raw':r['response']['status']='incomplete'
    if change=='validation':r['validation']['ifvg_validation']['stop']+=.25
    if change=='future':r['received_at']=T+100
    with pytest.raises((ValueError,KeyError,AssertionError)):ict.from_record(r,T+71,'IFVG')

@pytest.fixture
def ifvg_routed(routed,monkeypatch):
    f=routed;f.now=T+71;r=record();save(r,f.tmp.name)
    f.r=r;f.base=ict.from_record(r,f.now,'IFVG');f.order.update(f.base);f.prepare()
    p=ipolicy(f.sync.binding);p['ifvg_execution']=scope();Path(os.environ['ICT_EXECUTION_POLICY_FILE']).write_text(json.dumps(p))
    return f

@pytest.mark.parametrize('key,value',[('ICT_EXECUTION_ENABLED','false'),('EXEC_KILL_SWITCH','true')])
def test_family_activation_cannot_inherit_original(ifvg_routed,monkeypatch,key,value):
    f=ifvg_routed;monkeypatch.setenv(key,value)
    assert ict.permission(f.order,clock=lambda:f.now)
    with pytest.raises(broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert not f.posts

def test_original_policy_does_not_approve_ifvg(ifvg_routed):
    f=ifvg_routed;p=ipolicy(f.sync.binding);Path(os.environ['ICT_EXECUTION_POLICY_FILE']).write_text(json.dumps(p))
    assert ict.permission(f.order,clock=lambda:f.now)
    with pytest.raises(ValueError,match='ifvg_policy'):gate.policy_for(f.order)
    assert not f.posts

@pytest.mark.parametrize('short',[False,True])
def test_ifvg_native_ack_fill_oco_close(ifvg_routed,short):
    f=ifvg_routed
    if short:
        f.r=record(True);save(f.r,f.tmp.name);f.base=ict.from_record(f.r,f.now,'IFVG');f.order.update(f.base);f.prepare()
    result=f.submit();assert result['sent'];assert f.local()['position_json'] is None
    body=f.posts[-1][1];assert body['limitPrice']==f.order['entry_price'];assert body['stopLoss']==f.order['stop_price'];assert body['takeProfit']==f.order['target_price']
    f.open();assert f.t.row(f.order['order_id'])['state']=='OPEN'
    f.fixture_round_trip=ipolicy()['costs']['round_trip_usd']
    snap,net=f.complete();f.t.reconcile_one(f.t.row(f.order['order_id']),snap)
    assert f.t.row(f.order['order_id'])['state']=='CLOSED_PENDING_COSTS'
    f.t.reconcile_costs(snap);assert f.t.row(f.order['order_id'])['state']=='CLOSED'
    assert f.t.resolution(f.order['order_id'])['net']==pytest.approx(net)

def test_ifvg_timeout_restart_no_duplicate(ifvg_routed):
    f=ifvg_routed;f.timeout=True;assert f.t.submit(f.payload(),f.order['order_id'])['submission_unknown']
    f.t=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
    with pytest.raises(broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert len(f.posts)==1

def test_ifvg_cancel_late_fill_keeps_children(ifvg_routed):
    f=ifvg_routed;f.submit();f.now+=61;f.t.reconcile(f.snapshot());assert any(p.endswith('/11/cancel') for p,b in f.posts)
    f.open();assert f.local()['state']=='OPEN';before=len(f.posts)
    f.t=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now);f.t.reconcile(f.sync.snapshot())
    assert not any(p.endswith('/12/cancel') or p.endswith('/13/cancel') for p,b in f.posts[before:])

@pytest.mark.parametrize('short',[False,True])
def test_reinversion_and_missing_closed_bars(short):
    r=record(short);p=copy.deepcopy(r['packet']);p['as_of']=T+120
    inv=p['evidence']['inv'];lo,hi=(104,106) if short else (114,116)
    p['evidence']['next']=dict(inv,time=T+60,available_at=T+120,close=105 if short else 115)
    assert ict.ifvg_market_reason(r,p) is None
    p['evidence']['next']['close']=hi+.25 if short else lo-.25
    assert ict.ifvg_market_reason(r,p)=='ict_ifvg_reinverted_before_fill'
    del p['evidence']['next'];assert ict.ifvg_market_reason(r,p)=='ict_ifvg_post_inversion_data_gap'

def test_ifvg_durable_opportunity_and_duplicate_review(ifvg_routed,monkeypatch):
    f=ifvg_routed;monkeypatch.setattr(ict.time,'time',lambda:f.now);real=ict.permission
    monkeypatch.setattr(ict,'permission',lambda o:real(o,clock=lambda:f.now));monkeypatch.setattr(ict,'_CALLBACKS',{})
    submit=Mock(return_value=dict(state='SUBMISSION_UNKNOWN',reason='MOCK_TIMEOUT'));monkeypatch.setattr(ict,'dispatch',submit)
    assert ict.process(f.r,'IFVG')['state']=='SUBMISSION_UNKNOWN'
    assert ict.process(f.r,'IFVG')['state']=='SUBMISSION_UNKNOWN'
    other=copy.deepcopy(f.r);other['id']='ICT_INDEPENDENT_NYAM_20261030_200';save(other,f.tmp.name)
    ict.process(other,'IFVG');assert submit.call_count==1

def test_cloud_emits_contract_only_with_separate_enable(tmp_path,monkeypatch):
    from ict_shadow_cloud import Cloud
    p,_=setup();c=Cloud({'DATA_DIR':str(tmp_path)})
    assert 'ifvg_execution_contract' not in c.prepare(p,T+70)
    monkeypatch.setattr(ict,'ifvg_permission',lambda **kw:None)
    assert c.prepare(p,T+70)['ifvg_execution_contract']==ict.IFVG_VERSION
    s=research.schema('packet',ict.VERSION,True,ict.IFVG_VERSION)
    assert s['properties']['ifvg_review']['properties']['plan']['anyOf'][1]['properties']['invalidation_mode']['enum'][0]==ict.IFVG_VERSION

def test_handoff_reads_parent_routes_companion_once(tmp_path,monkeypatch):
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');r=record()
    c=handoff.Handoff(tmp_path,{'AB_V3_CONTRACT':'MNQZ2026','ICT_EXECUTION_ENABLED':'true'},sizing=lambda *a:{'allowed':True,'max_quantity':1})
    process=Mock(return_value={'state':'BLOCKED','reason':'mock'});monkeypatch.setattr(ict,'process',process)
    c.refresh([r],r['packet'],T+71)
    assert process.call_args.kwargs=={'family':'IFVG'}
    assert c.state()['intents'][0]['review_id']==r['id']+':IFVG'
    assert c.state()['intents'][0]['plan']['family']=='IFVG'

@pytest.mark.parametrize('after',[0,True,'0',int((T+62)*1000),int((T+100)*1000)])
def test_separate_forward_epoch(ifvg_routed,after):
    f=ifvg_routed;p=Path(os.environ['ICT_EXECUTION_POLICY_FILE']);q=json.loads(p.read_text());q['ifvg_execution']['armed_after_ms']=after;p.write_text(json.dumps(q))
    assert ict.permission(f.order,clock=lambda:f.now)
    with pytest.raises(broker.TransportError):f.t.submit(f.payload(),f.order['order_id'])
    assert not f.posts

def test_ifvg_dispatch_uses_existing_shared_guard(ifvg_routed,monkeypatch):
    from test_ict_execution import test_dispatch_uses_shared_guard_size_and_native_route
    test_dispatch_uses_shared_guard_size_and_native_route(ifvg_routed,monkeypatch)

def test_ifvg_operator_cannot_change_policy_without_exact_human_phrase(tmp_path,monkeypatch):
    import ict_ifvg_operator as op
    p=tmp_path/'policy.json';p.write_text(json.dumps({'approved':True,'existing':'unchanged'}));before=p.read_bytes()
    monkeypatch.setenv('ICT_EXECUTION_POLICY_FILE',str(p));monkeypatch.setattr(op.sys,'stdin',type('TTY',(),{'isatty':lambda s:True})())
    monkeypatch.setattr(op,'check',lambda:{'mock':True});monkeypatch.setattr('builtins.input',lambda _: 'yes')
    with pytest.raises(ValueError,match='Cancelled'):op.write_scope(True)
    assert p.read_bytes()==before

def test_mock_operator_scope_preserves_account_and_global_policy(tmp_path,monkeypatch):
    import ict_ifvg_operator as op
    p=tmp_path/'policy.json';p.write_text(json.dumps({'approved':True,'existing':'unchanged'}))
    monkeypatch.setenv('ICT_EXECUTION_POLICY_FILE',str(p));monkeypatch.setattr(op.sys,'stdin',type('TTY',(),{'isatty':lambda s:True})())
    monkeypatch.setattr(op,'check',lambda:{'mock':True});monkeypatch.setattr('builtins.input',lambda _: op.PHRASE)
    env=dict(os.environ);op.write_scope(True);q=json.loads(p.read_text())
    assert q['approved'] is True and q['existing']=='unchanged' and q['ifvg_execution']['approved'] is True
    assert q['ifvg_execution']['broker_round_trip_verified'] is False and dict(os.environ)==env
    monkeypatch.setattr('builtins.input',lambda _: 'STOP IFVG ENTRIES');op.write_scope(False)
    q=json.loads(p.read_text());assert q['approved'] is True and q['ifvg_execution']['approved'] is False
