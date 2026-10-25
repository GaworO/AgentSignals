"""Explicit resting LIMIT admission. All AI/account/broker examples are MOCKS."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import execution_validation as gate
import directional_resting_limit as resting
import directional_market_data as market
import ab_v3_live
import test_resting_limit_lifecycle as lifecycle
from test_execution_readiness import evidence


def policy():
    p=json.loads((Path(__file__).parents[1]/'directional_resting_limit_policy.json').read_text())
    p['broker_workflow_verification']={}
    p.update(account_binding='simulator',validation_reference='SYNTHETIC_TEST_ONLY',
             residual_risk_acceptance_reference='SYNTHETIC_TEST_ONLY')
    return p


def case(direction='LONG'):
    a=evidence();a[0].update(quantity=1,direction=direction)
    if direction=='SHORT':a[0].update(stop_price=110.,target_price=80.)
    a[4]['setup']=dict(a[0]);a[5]=policy()
    a[1]=dict(source=resting.SOURCE,order_id='one',contract='MNQZ2026',closed=True,feed_authenticated=True,
        market_open=True,evidence_id='MOCK_CANDLE',bar_open_ms=900000,bar_close_ms=960000,received_ms=960010,
        checked_ms=a[-1],ohlc=dict(open=100.,high=105.,low=95.,close=104.))
    return a


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_waiting_limit_not_required_to_be_immediately_executable(direction):
    a=case(direction)
    assert gate.evaluate(*a) is None
    observation=resting.assessment(a[0],a[1],a[5])
    assert observation['executable_price'] is None and observation['bid'] is None and observation['ask'] is None
    assert observation['spread_ticks'] is None and not observation['executable_quote_verified']
    assert observation['reward_risk_at_original_limit']['gross_rr']==2
    assert observation['reward_risk_at_original_limit']['net_rr']<2
    assert a[0]['entry_price']==100 and a[0]['quantity']==1


@pytest.mark.parametrize('index,key,value,reason',[
 (0,'quantity',2,'resting_limit_quantity_cap'),
 (0,'quantity',True,'resting_limit_quantity_cap'),
 (0,'expiry_ms',1000000,'signal_expired'),
 (0,'activation_ms',800000,'signal_too_old'),
 (0,'contract','MNQH2027','contract_mismatch'),
 (0,'target_price',130.,'original_fixed_2r_required'),
 (0,'stop_price',100.,'bracket_invalidated'),
 (0,'entry_price',100.1,'off_tick_price'),
 (1,'contract','NQZ2026','resting_limit_closed_candle_evidence_required'),
 (1,'source','collective','resting_limit_closed_candle_evidence_required'),
 (1,'source','drafted','resting_limit_closed_candle_evidence_required'),
 (1,'closed',False,'resting_limit_closed_candle_evidence_required'),
 (1,'feed_authenticated',False,'resting_limit_authenticated_feed_required'),
 (1,'received_ms',1000001,'resting_limit_candle_stale_or_future'),
 (1,'received_ms',959999,'resting_limit_candle_stale_or_future'),
 (1,'market_open',False,'market_closed_or_unknown'),
 (1,'checked_ms',996999,'resting_limit_observation_stale'),
 (2,'valid',False,'directional_thesis_not_revalidated'),
 (3,'allowed',False,'account_risk_not_revalidated'),
 (3,'checked_ms',996999,'account_risk_not_revalidated'),
 (3,'account_binding','foreign','resting_limit_account_binding_mismatch'),
 (4,'state','WAIT','setup_review_missing_or_expired'),
 (4,'state','REJECT','setup_review_missing_or_expired'),
 (4,'state','TIMEOUT','setup_review_missing_or_expired'),
 (4,'expires_at',1000,'setup_review_missing_or_expired'),
 (4,'available_at',1001,'setup_review_missing_or_expired'),
 (4,'order_id','another','setup_review_missing_or_expired')])
def test_closed_candle_policy_preserves_safety_gates(index,key,value,reason):
    a=case();a[index][key]=value
    assert gate.evaluate(*a)==reason


@pytest.mark.parametrize('key',['entry_price','stop_price','target_price','direction','contract','expiry_ms'])
def test_exact_ai_geometry_binding_survives_new_market_policy(key):
    a=case();a[4]['setup'][key]='CHANGED'
    assert gate.evaluate(*a)=='setup_review_geometry_mismatch'


def test_missing_review_stale_candles_and_rollover_are_not_revived():
    a=case();a[4]={};assert gate.evaluate(*a)=='setup_review_missing_or_expired'
    a=case();a[1].update(bar_open_ms=840000,bar_close_ms=900000)
    assert gate.evaluate(*a)=='resting_limit_candle_stale_or_future'
    a=case();a[-1]=1020000
    assert gate.evaluate(*a)=='resting_limit_latest_closed_minute_missing'
    a=case();a[1]['ohlc']['low']=200
    assert gate.evaluate(*a)=='resting_limit_invalid_candle'
    a=case();a[5]['contract_last_trade_ms']=1000000
    assert gate.evaluate(*a)=='contract_expired'


@pytest.mark.parametrize('key,value',[
 ('approved',False),('accepted_residual_risks',[]),('account_binding',None),
 ('max_quantity',41),('advanced_writes',True),('allow_scale_in',True),('fixed_gross_rr',3),
 ('quote_source','collective'),('max_account_age_ms',3001),('max_closed_m1_age_ms',75001),
 ('max_signal_age_ms',120001),('max_pending_lifetime_sec',601),
 ('execution_model','typo'),('max_thesis_age_ms',float('nan'))])
def test_policy_must_be_explicit_and_cannot_expand_pilot(key,value):
    p=policy();p[key]=value
    with pytest.raises(ValueError):gate.validate_parameters(p)


def test_quote_policy_has_no_automatic_candle_fallback():
    a=evidence();a[1]=case()[1]
    assert gate.evaluate(*a)=='quote_source_unverified'


@pytest.fixture
def candle_runtime(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026');monkeypatch.setenv('EXEC_TICKER','MNQZ2026')
    monkeypatch.setenv('AB_V3_FEED_TOKEN','test-only-token');monkeypatch.setenv('AB_V3_TV_MAPPING_VERIFIED','1')
    monkeypatch.setenv('AB_V3_DETECTOR_CONTRACT_VERIFIED','1')
    path=tmp_path/'policy.json';path.write_text(json.dumps(policy()))
    monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(path))
    now=[1000.]
    raw=dict(ts_event='1970-01-01T00:15:00+00:00',open=100.,high=105.,low=95.,close=104.,volume=20.)
    ab_v3_live.ingest(dict(tf='M1',contract='MNQZ2026',bars=[raw]),current=960010,notify_m1=False)
    client=market.MarketSource(clock=lambda:now[0],market_open=lambda:True,reader=Mock(side_effect=AssertionError('NO_QUOTE_HTTP_ALLOWED')))
    return client,now,path


def test_real_immutable_bar_adapter_uses_no_quote_http(candle_runtime):
    client,now,path=candle_runtime;a=case();a[1]=client(a[0])
    assert gate.evaluate(*a) is None
    now[0]=1020
    with pytest.raises(ValueError,match='latest_closed_minute_missing'):client(a[0])


@pytest.mark.parametrize('flag',['AB_V3_FEED_TOKEN','AB_V3_TV_MAPPING_VERIFIED','AB_V3_DETECTOR_CONTRACT_VERIFIED','EXEC_TICKER'])
def test_feed_auth_and_contract_identity_required(candle_runtime,monkeypatch,flag):
    monkeypatch.delenv(flag)
    with pytest.raises(ValueError):candle_runtime[0](case()[0])


def test_future_bar_is_rejected_at_ingestion(candle_runtime):
    raw=dict(ts_event='1970-01-01T00:16:00Z',open=100.,high=105.,low=95.,close=104.,volume=20.)
    with pytest.raises(ValueError,match='unclosed'):ab_v3_live.ingest(dict(tf='M1',contract='MNQZ2026',bars=[raw]),current=1000000)


def test_monitor_does_not_poll_unsupported_nt8_in_cloud_policy(candle_runtime,monkeypatch):
    monkeypatch.setenv('DIRECTIONAL_QUOTE_MONITOR_ENABLED','true')
    monkeypatch.setattr(market.QuoteClient,'fetch',Mock(side_effect=AssertionError('NO_NT8')))
    assert market.monitor_once() is None


def test_runtime_status_does_not_report_bbo_as_required_but_keeps_risk_and_proof(candle_runtime,monkeypatch):
    monkeypatch.setattr(gate,'_SOURCES',dict(market=lambda x:{},thesis=lambda x:{},risk=lambda x:{}))
    monkeypatch.setattr(ab_v3_live,'native_fixed_bracket',lambda:True)
    monkeypatch.delenv('MFFU_RISK_EVIDENCE_FILE',raising=False)
    s=gate.status()
    assert s['policy_error'] is None and not s['ready'] and not s['executable_quotes_required']
    assert s['candle_quote_fallback'] is False
    assert 'native_bracket_broker_verification_missing' in s['access_blockers']
    assert 'mffu_current_allowances_missing' in s['access_blockers']
    assert 'executable_quote_access_not_configured' not in s['access_blockers']


@pytest.fixture
def native_one():
    f=lifecycle.fixture.NativeTests();f.setUp();f.order.update(quantity=1,target_price=30020.);f.prepare()
    payload=f.payload;f.payload=lambda:dict(payload(),quantity=1)
    try:yield f
    finally:f.doCleanups()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_native_improved_fill_keeps_original_stop_target_and_reconciles_cost_risk(native_one,tmp_path,monkeypatch,direction):
    f=native_one;sign=1 if direction=='LONG' else -1
    f.order.update(direction=direction,stop_price=30000-sign*10,target_price=30000+sign*20);f.prepare()
    p=policy();p['account_binding']=f.binding if hasattr(f,'binding') else f.t.binding
    path=tmp_path/'policy.json';path.write_text(json.dumps(p));monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(path))
    lifecycle.submit(f)
    f.now+=1;f.t.reconcile(lifecycle.fill(f,price=30000-sign*.5))
    obs=json.loads(f.t.row('order-one')['observation_json']);r=obs['actual_fill_risk']
    assert obs['protection_verified'] and obs['stop_price']==f.order['stop_price'] and obs['target_price']==f.order['target_price']
    assert r['gross_risk_usd']==19 and r['gross_rr']>2
    assert r['total_estimated_stop_loss_usd']==pytest.approx(22.24)
    assert len(f.posts)==1


def test_pending_lifetime_cannot_exceed_policy_even_with_long_setup_and_payload(native_one,tmp_path,monkeypatch):
    f=native_one;f.order['expiry_ms']=int(f.now*1000)+3600000;f.prepare()
    p=policy();p['max_pending_lifetime_sec']=10
    path=tmp_path/'policy.json';path.write_text(json.dumps(p));monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(path))
    lifecycle.submit(f)
    assert f.t.row('order-one')['expiry_ms']<=int(f.now*1000)+10000


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('decision',['ALLOW','WAIT','REJECT','TIMEOUT'])
def test_new_policy_reaches_native_order_only_after_exact_mock_review(native_one,tmp_path,monkeypatch,direction,decision):
    f=native_one;sign=1 if direction=='LONG' else -1
    f.order.update(direction=direction,stop_price=30000-sign*10,target_price=30000+sign*20);f.prepare()
    now=int(f.now*1000);end=now//60000*60000
    p=policy();p['account_binding']=f.sync.binding
    path=tmp_path/'policy.json';path.write_text(json.dumps(p));monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(path))
    a=case(direction);a[0]=dict(f.order);a[5]=p;a[-1]=now
    a[1].update(order_id=f.order['order_id'],bar_open_ms=end-60000,bar_close_ms=end,received_ms=end+1,checked_ms=now,
        ohlc=dict(open=30000.,high=30005.,low=29995.,close=30004.))
    a[2].update(order_id=f.order['order_id'],checked_ms=now)
    a[3].update(order_id=f.order['order_id'],checked_ms=now,account_binding=f.sync.binding)
    a[4].update(order_id=f.order['order_id'],setup=dict(f.order),state=decision,available_at=f.now-1,expires_at=f.now+60)
    f.final_gate.side_effect=lambda order,review:gate.evaluate(order,*a[1:])
    result=f.submit() if decision=='ALLOW' else f.t.submit(f.payload(),f.order['order_id'])
    assert bool(result['sent'])==(decision=='ALLOW')
    if decision!='ALLOW':
        assert f.posts==[] and result['reason']=='setup_review_missing_or_expired'
    else:
        assert f.local()['position_json'] is None  # ACK is never a fill.
        for life in f.life.values():life['version']['orderQty']=1
        f.accept(f.order,dict(state='SENT',quantity=1,broker=result))
        f.now+=1;snapshot=lifecycle.fill(f);f.t.reconcile(snapshot)
        entry=snapshot['fills'][0]
        obs=json.loads(f.t.row('order-one')['observation_json'])
        assert obs['protection_verified'] and obs['oco_verified']
        assert obs['remaining_quantity']==1 and set(obs['protective_remaining_quantities'].values())=={1}
        assert obs['actual_fill_risk']['gross_rr']==2
        # Existing closure and accounting use actual broker evidence fixtures.
        f.now+=5
        for oid in (11,12,13):f.life[oid]['order']['ordStatus']='Filled' if oid in (11,12) else 'Canceled'
        exit=f.fill(102,12,1,f.order['target_price'],'Sell' if sign==1 else 'Buy')
        net=40.-2.4
        f.account['balance'].update(netLiq=48961.55+net,totalCashValue=48961.55+net)
        f.history=[dict(fillId=x['id'],orderId=x['orderId'],accountId=2214,accountName='Builder account',environment='demo',
                       instrument='MNQ 12-26',qty=1,price=x['price'],action=x['action'],timestamp=x['timestamp'],commission=.6,fees=.6)
                   for x in (entry,exit)]
        f.t.reconcile(f.snapshot([entry,exit]))
        assert f.local()['state']=='CLOSED'  and f.t.row('order-one')['state']=='CLOSED'
        assert net>0


def accepted_pilot(monkeypatch):
    import hashlib
    import builder_broker_sync
    p=json.loads((Path(__file__).parents[1]/'directional_resting_limit_policy.json').read_text())
    identity=['1','SYNTHETIC_PILOT','demo']
    binding=hashlib.sha256(builder_broker_sync.encode(identity).encode()).hexdigest()
    p['account_binding']=p['broker_workflow_verification']['account_binding']=binding
    monkeypatch.setattr(gate,'policy',lambda:p)
    for k,v in dict(BUILDER_BROKER_ACCOUNT_ID=identity[0],BUILDER_BROKER_ACCOUNT_NAME=identity[1],BUILDER_BROKER_ENVIRONMENT=identity[2],
            AB_V3_CONTRACT='MNQZ2026',EXEC_MAX_QTY='1',EXEC_STRATEGY_POLICY='V3_ONLY',AB_ENTRY_ARMED='0',AB_MANAGER_MODE='shadow',
            V3_EXIT_MODE='shadow',V3_EXIT_REAL_EXECUTION='false',V3_RUNNER_ENABLED='false').items():monkeypatch.setenv(k,v)
    return p


def test_operator_accepted_history_is_not_misrepresented_as_independent_recovery(monkeypatch):
    p=accepted_pilot(monkeypatch)
    assert ab_v3_live.native_bracket_readiness() is None
    assert p['broker_workflow_verification']['current_directional_broker_run_verified'] is False
    assert p['broker_workflow_verification']['recovery_evidence_kind']=='DETERMINISTIC_TESTS_NOT_PROVIDER_VERIFIED'


@pytest.mark.parametrize('key,value',[
 ('EXEC_MAX_QTY','2'),('EXEC_STRATEGY_POLICY','AB_ONLY'),('AB_ENTRY_ARMED','1'),('AB_MANAGER_MODE','live'),
 ('V3_EXIT_MODE','real'),('V3_EXIT_REAL_EXECUTION','true'),('V3_RUNNER_ENABLED','true'),('BUILDER_BROKER_ACCOUNT_ID','2')])
def test_history_pilot_does_not_authorize_other_accounts_or_advanced_writes(monkeypatch,key,value):
    accepted_pilot(monkeypatch);monkeypatch.setenv(key,value)
    assert ab_v3_live.native_bracket_readiness()=='native_bracket_controlled_pilot_scope_unverified'


@pytest.mark.parametrize('key,value',[
 ('historical_native_oso_oco_verified',False),('current_directional_broker_run_verified',True),
 ('operator_pilot_acceptance_reference',None),('recovery_evidence_reference',None),
 ('broker_order_ids',[1,1,1]),('contract','MNQH2027')])
def test_pilot_evidence_scope_cannot_be_forged_by_ordinary_flags(monkeypatch,key,value):
    p=accepted_pilot(monkeypatch);p['broker_workflow_verification'][key]=value
    assert ab_v3_live.native_bracket_readiness()=='native_bracket_controlled_pilot_scope_unverified'
