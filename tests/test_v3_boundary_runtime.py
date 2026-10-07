"""Offline lifecycle/inference checks: never use an execution webhook."""
import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

import ab_v3_live as live
import v3_boundary as boundary
import v3_boundary_features as features
import v3_position_manager as pm
import v3_runner as runner


@pytest.fixture
def env(tmp_path,monkeypatch):
    values=dict(AB_V3_DB=str(tmp_path/'v3.sqlite3'),DATA_DIR=str(tmp_path),AB_V3_MODE='LIVE',
                AB_V3_CONTRACT='MNQZ2026',EXEC_TICKER='MNQZ2026',ACCOUNT_LABEL='test',
                AB_V3_FEED_TOKEN='test-feed',AB_V3_POSITION_TOKEN='test-position',
                AB_V3_EXCLUSIVE_ROUTE='1',AB_V3_DETECTOR_CONTRACT_VERIFIED='1',PRICE_OFFSET='0',
                EXEC_WEBHOOK='https://example.invalid',V3_RUNNER_ENABLED='true',V3_EXIT_MODE='real',
                V3_EXIT_REAL_EXECUTION='true',V3_EXIT_LIVE_PARITY_VERIFIED='true',
                V3_EXIT_SHADOW_VERIFIED='true',V3_EXIT_STAGING_CLOSE_VERIFIED='true')
    for k,v in values.items():monkeypatch.setenv(k,v)
    monkeypatch.delenv('AB_V3_BROKER_TOKEN',raising=False)
    monkeypatch.setattr(live,'_ROUTE_CALLBACK',lambda:'route-test')
    monkeypatch.setattr(pm.threading.Thread,'start',lambda self:None)
    import requests
    monkeypatch.setattr(requests,'post',lambda *a,**k:pytest.fail('Unexpected network request'))
    clock=[int(pd.Timestamp('2026-10-07T14:00Z').timestamp()*1000)]
    monkeypatch.setattr(live,'now_ms',lambda:clock[0])
    o=dict(order_id='T1',strategy='AB_DIRECTIONAL',direction='LONG',activation_ms=clock[0],
           expiry_ms=clock[0]+600000,entry_price=100.,stop_price=90.,target_price=120.,risk_points=10.,
           payload_json=json.dumps(dict(v3_snapshot=dict(source_level=99.,fvg_edge=99.))))
    return o,clock


def get_order():
    with live.connect() as c:return dict(c.execute("SELECT * FROM orders WHERE order_id='T1'").fetchone())


def accept(o):live.record_dispatch(o,dict(state='SENT',quantity=2))


def event(o,clock,kind,**extras):
    return dict(event=kind,event_id=kind+str(clock[0]),event_ms=clock[0],order_id=o['order_id'],
                route_id='route-test',contract='MNQZ2026',account_label='test',**extras)


def bar(ms,high=101.,low=99.,close=100.):
    return dict(ts_event=pd.Timestamp(ms,unit='ms',tz='UTC').isoformat(),open=100.,high=high,low=low,close=close,volume=1.)


def feed(o,clock,touch=False,stop=False):
    start=o['activation_ms'];clock[0]=start+180000+2000
    live.ingest(dict(contract=live.contract(),tf='M1',bars=[bar(start+i*60000) for i in range(3)]))
    sec=[bar(start+120000+i*1000,high=120. if touch and i==20 else 101.,low=89. if stop and i==25 else 99.) for i in range(62)]
    live.ingest(dict(contract=live.contract(),tf='1s',bars=sec))


def m3(known,reason=None):
    return dict(known_ms=known,first_events={},signals=dict(primary_manager='M3' if reason else None,
                primary_reason=reason,triggered_managers=['M3'] if reason else [],triggered_reasons=[reason] if reason else []))


def set_decisions(monkeypatch,classification,reason):
    monkeypatch.setattr(boundary,'evaluate',lambda o,p,m,k:[dict(known_ms=k,classification=classification,win_score=.6,loss_score=.8,boundary_score=.2)])
    monkeypatch.setattr(pm.frozen,'evaluate',lambda o,p,m,k,enabled:m3(k,reason))


def test_entry_accepted_immediately_assumed_without_position(env):
    o,clock=env;accept(o);r=get_order()
    assert r['state']=='ASSUMED_OPEN'
    p=json.loads(r['position_json']);assert p['fill_ms']==clock[0] and p['assumed']
    assert json.loads(r['order_json'])['target_price']==130.
    accept(o);assert get_order()['position_json']==r['position_json']


@pytest.mark.parametrize('kind',pm.INVALIDATIONS)
def test_explicit_invalidation_removes_active_state_and_cannot_resurrect(env,kind):
    o,clock=env;accept(o);clock[0]+=1000
    live.broker_event(event(o,clock,kind))
    assert get_order()['state']==kind and get_order()['position_json'] is None
    accept(o);assert get_order()['state']==kind
    pm.monitor();live.dispatch_frozen_exits()
    with live.connect() as c:assert c.execute('SELECT COUNT(*) FROM actions').fetchone()[0]==0


def test_invalidation_before_dispatch_callback(env):
    o,clock=env;live.prepare(o);clock[0]+=1000;live.broker_event(event(o,clock,'NO_FILL'));accept(o)
    assert get_order()['state']=='NO_FILL'


def test_expiry_clock_does_not_invalidate_assumed(env):
    o,clock=env;accept(o);clock[0]=o['expiry_ms']+60000;pm.monitor()
    assert get_order()['state']=='ASSUMED_OPEN'


def test_real_position_promotes_without_waiting_at_entry(env):
    o,clock=env;accept(o);clock[0]+=2000
    live.broker_event(event(o,clock,'POSITION',quantity=2,position_id='real1',direction='LONG',
                      entry_price=100.,stop_price=90.,target_price=130.,fill_ms=o['activation_ms']+1000,exclusive=True))
    assert get_order()['state']=='BROKER_CONFIRMED_OPEN'
    assert json.loads(get_order()['position_json'])['assumed'] is False


@pytest.mark.parametrize('classification,reason,expected',[
    ('FAILURE',None,'HOLD'),('FAILURE','F2','CLOSE'),('RECOVERY','B11','HOLD'),
    ('AMBIGUOUS','B11','CLOSE'),('NO_BOUNDARY_YET','OBFH_EXPANSION','CLOSE'),
    ('FAILURE','OBFH_3_CLOSES_10M','CLOSE')])
def test_boundary_reaches_manager_and_m3_gating(env,monkeypatch,classification,reason,expected):
    o,clock=env;accept(o);feed(o,clock);set_decisions(monkeypatch,classification,reason);pm.monitor()
    with live.connect() as c:
        result=json.loads(c.execute('SELECT payload_json FROM manager_states').fetchone()[0])
        ex=c.execute('SELECT * FROM exit_trades').fetchone()
    assert result['decision']==expected and result['boundary']['classification']==classification
    assert result['boundary']['known_ms']==o['activation_ms']+180000
    assert result['boundary']['win_score']==.6
    assert (ex['status']=='EXIT_SIGNALLED')==(expected=='CLOSE')


def test_failure_close_dispatches_without_broker_snapshot_and_closes_assumed(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock);set_decisions(monkeypatch,'FAILURE','F2');pm.monitor()
    send=Mock(return_value=Mock(status_code=200,json=lambda:dict(success=True,id='exit1')))
    monkeypatch.setattr(live,'_send_broker_exit',send);live.dispatch_frozen_exits()
    assert send.call_count==1 and get_order()['state']=='CLOSED'
    assert json.loads(get_order()['position_json'])['closure_basis']=='ASSUMED_EXIT_ACCEPTED'
    with live.connect() as c:assert c.execute('SELECT exit_pnl FROM exit_trades').fetchone()[0] is None
    live.dispatch_frozen_exits();assert send.call_count==1


@pytest.mark.parametrize('probability,kind',[(.751,'HOLD_3R'),(.75,'EXIT_2R'),(.2,'EXIT_2R')])
def test_runner_first_2r_uses_strict_threshold(env,monkeypatch,probability,kind):
    o,clock=env;accept(o);feed(o,clock,touch=True);set_decisions(monkeypatch,'RECOVERY',None)
    monkeypatch.setattr(runner,'score',lambda values:probability);pm.monitor();pm.monitor()
    with live.connect() as c:
        rows=c.execute('SELECT payload_json FROM runner_decisions').fetchall()
        ex=c.execute('SELECT status FROM exit_trades').fetchone()[0]
    assert len(rows)==1 and json.loads(rows[0][0])['decision_kind']==kind
    assert (ex=='EXIT_SIGNALLED')==(kind=='EXIT_2R')


def test_no_fill_during_scoring_discards_manager_and_runner(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock,touch=True)
    def classify(o_,p,m,k):
        live.broker_event(event(o,clock,'NO_FILL'))
        return [dict(known_ms=k,classification='FAILURE',win_score=.8,loss_score=1.)]
    monkeypatch.setattr(boundary,'evaluate',classify);monkeypatch.setattr(runner,'score',lambda f:.2)
    pm.monitor();live.dispatch_frozen_exits()
    assert get_order()['state']=='NO_FILL'
    with live.connect() as c:
        for table in ('manager_states','runner_decisions','actions','boundary_states'):
            assert c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


def test_no_fill_between_action_creation_and_http_prevents_send(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock);set_decisions(monkeypatch,'FAILURE','F2');pm.monitor()
    send=Mock();monkeypatch.setattr(live,'_send_broker_exit',send)
    original=pm.send_exit
    def race(aid,oid,payload):
        live.broker_event(event(o,clock,'NO_FILL'));original(aid,oid,payload)
    monkeypatch.setattr(pm,'send_exit',race);live.dispatch_frozen_exits()
    assert not send.called and get_order()['state']=='NO_FILL'


def test_guard_and_stop_override_recovery(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock,stop=True);set_decisions(monkeypatch,'RECOVERY','F2');pm.monitor()
    assert get_order()['state']=='CLOSED'
    assert json.loads(get_order()['position_json'])['closure_basis']=='ASSUMED_BRACKET_SL'


def test_guard_takes_ownership_before_ack(env):
    o,clock=env;accept(o);pm.guard_exit_requested('risk');pm.monitor()
    assert get_order()['state']=='GUARD_EXIT_PENDING'
    pm.guard_exit_accepted();assert get_order()['state']=='CLOSED'


def test_unknown_exit_is_not_retried(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock);set_decisions(monkeypatch,'FAILURE','F2');pm.monitor()
    send=Mock(side_effect=TimeoutError);monkeypatch.setattr(live,'_send_broker_exit',send)
    live.dispatch_frozen_exits();live.dispatch_frozen_exits();pm.monitor()
    assert send.call_count==1 and get_order()['state']=='EXIT_UNKNOWN'


def test_entry_filters_unchanged(env):
    import continuation_live
    o,_=env;o['candidate_payload_json']=json.dumps(dict(brk=1))
    assert continuation_live._v3_entry_filter(o)=='v3_first_break'
    o['candidate_payload_json']=json.dumps(dict(brk=2));o['stop_price']=90.25
    assert continuation_live._v3_entry_filter(o)=='v3_initial_stop_lt_10_points'
    o['stop_price']=90.;assert continuation_live._v3_entry_filter(o) is None


def test_models_and_validated_m3_runner_hashes_unchanged():
    assert live.frozen_parity_ok() and live.runner_parity_ok()
    assert [len(x['features']) for x in boundary.models()]==[38,305,24]


def test_frozen_feature_and_score_parity_and_future_erasure():
    with gzip.open(Path(__file__).parent/'fixtures/v3_boundary_frozen.json.gz','rt') as f:d=json.load(f)
    raw=np.array(d['bars']);m=pd.DataFrame(raw[:,1:],index=pd.to_datetime(raw[:,0],unit='ms',utc=True),columns=['open','high','low','close','volume'])
    result=features.build(d['order'],d['position'],m,d['known_ms'])
    expected=pd.DataFrame(d['expected']);names=boundary.models()[1]['features']
    np.testing.assert_allclose(result[names],expected[names],atol=1e-10,rtol=1e-10)
    assert result.manager_rules.tolist()==expected.manager_rules.tolist()
    scores=boundary.classify(result)
    for a,b in zip(scores,d['scores']):
        assert a['classification']==b['classification']
        assert a['win_score']==pytest.approx(b['win_score'],abs=1e-12)
        assert a['loss_score']==pytest.approx(b['loss_score'],abs=1e-12)
    # Add an extreme unfinished/future candle. It must not enter any feature.
    future=m.iloc[[-1]].copy();future.index+=pd.Timedelta(minutes=1);future.iloc[:,:4]=1e6
    again=features.build(d['order'],d['position'],pd.concat([m,future]),d['known_ms'])
    np.testing.assert_array_equal(result[names],again[names])


@pytest.mark.parametrize('win,loss,probability,expected',[(.5,.75,.8,'RECOVERY'),(.5,.75,.2,'FAILURE'),(.5,.75,.5,'AMBIGUOUS'),(.49,1.,.8,'NO_BOUNDARY_YET'),(.8,.74,.8,'NO_BOUNDARY_YET')])
def test_high_high_gate_and_frozen_classification_thresholds(monkeypatch,win,loss,probability,expected):
    class Model:
        def predict_proba(self,x):return np.tile([1-probability,probability],(len(x),1))
    bm=dict(model=Model(),bootstrap=[Model(),Model()],features=boundary.FEATURES)
    row=dict.fromkeys(['current_r','mfe_r','gb_current_drawdown_r','ne','entry_fvg_broken','opp_fvg_held','seq_allowed'],0.)
    row.update(order_id='T',known_ms=600000,manager_rules='')
    monkeypatch.setattr(boundary,'models',lambda:({'features':[]},{'features':[]},bm))
    monkeypatch.setattr(boundary,'score_win',lambda b,d:(np.repeat(win,len(d)),[]))
    monkeypatch.setattr(boundary,'score_loss',lambda b,d:(np.repeat(loss,len(d)),np.zeros(len(d)),np.zeros(len(d))))
    out=boundary.classify(pd.DataFrame([row]))[0]
    assert out['classification']==expected and out['known_ms']==600000


def test_frozen_boundary_scores_match_all_three_historical_classes():
    with gzip.open(Path(__file__).parent/'fixtures/v3_boundary_frozen.json.gz','rt') as f:d=json.load(f)
    for case in d['score_cases']:
        actual=boundary.classify(pd.DataFrame(case['states']))[-1];expected=case['expected']
        assert actual['classification']==expected['classification']
        for k in ('win_score','loss_score','boundary_score','bootstrap_lo','bootstrap_hi'):
            assert actual[k]==pytest.approx(expected[k],abs=1e-12)


def test_entry_and_prepared_dispatch_need_no_broker_flat(env):
    o,clock=env;feed(o,clock)
    assert live.entry_blocker(o) is None
    live.prepare(o,claim=True)
    signal=dict(_v3_directional=True,_continuation_order_id='T1',entry=100.,SL=90.,TP=130.,dir='LONG')
    assert live.prepared_entry_blocker(signal) is None


def test_invalidation_endpoint_authenticated_without_broker_api(env):
    from flask import Flask
    o,clock=env;accept(o);clock[0]+=1000
    app=Flask(__name__);live.register(app,route_callback=lambda:'route-test');client=app.test_client()
    body=event(o,clock,'NO_FILL')
    assert client.post('/ab/v3/broker',json=body).status_code==401
    assert client.post('/ab/v3/broker',json=body,headers={'X-V3-Position-Token':'test-position'}).status_code==200
    assert get_order()['position_json'] is None


def test_runner_invalidation_after_scoring_before_dispatch(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock,touch=True);set_decisions(monkeypatch,'AMBIGUOUS',None)
    monkeypatch.setattr(runner,'score',lambda f:.75);pm.monitor()
    live.broker_event(event(o,clock,'NO_FILL'))
    send=Mock();monkeypatch.setattr(live,'_send_broker_exit',send)
    live.dispatch_frozen_exits();pm.monitor()
    assert not send.called and get_order()['state']=='NO_FILL'


def test_first_2r_then_3r_closes_only_active_assumed(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock,touch=True);set_decisions(monkeypatch,'RECOVERY',None)
    monkeypatch.setattr(runner,'score',lambda f:.9);pm.monitor()
    clock[0]+=1000;live.ingest(dict(contract=live.contract(),tf='1s',bars=[bar(clock[0]-1000,high=130.)]))
    pm.monitor()
    assert get_order()['state']=='CLOSED'
    assert json.loads(get_order()['position_json'])['closure_basis']=='ASSUMED_BRACKET_TARGET'


def test_runner_missing_seconds_cannot_select_hold(env,monkeypatch):
    o,clock=env;accept(o);feed(o,clock,touch=True);set_decisions(monkeypatch,'NO_BOUNDARY_YET',None)
    with live.connect() as c:c.execute("DELETE FROM bars WHERE tf='1s' AND ts_ms=?",(o['activation_ms']+121000,))
    score=Mock(return_value=.99);monkeypatch.setattr(runner,'score',score);pm.monitor()
    with live.connect() as c:r=json.loads(c.execute('SELECT payload_json FROM runner_decisions').fetchone()[0])
    assert r['decision_kind']=='EXIT_2R' and r['error']=='runner_touch_seconds_gap' and not score.called


def test_feed_arriving_during_tick_is_drained(env,monkeypatch):
    count=[]
    def monitor():
        count.append(1)
        if len(count)==1:live._tick()
    monkeypatch.setattr(pm,'monitor',monitor)
    for name in ('monitor','monitor_runner','monitor_frozen','signal_runner_exits','dispatch_frozen_exits','dispatch_exits'):
        monkeypatch.setattr(live,name,lambda:None)
    live._tick();assert len(count)==2


def test_guard_pending_entry_cannot_be_reopened_by_late_sent(env):
    o,_=env;live.prepare(o);pm.guard_exit_requested('risk');accept(o)
    assert get_order()['state']=='GUARD_EXIT_PENDING'


@pytest.mark.parametrize('side,prefix', [('LONG','bsl'),('SHORT','ssl')])
def test_live_snapshot_supplies_causal_boundary_metadata(side,prefix):
    from types import SimpleNamespace
    import ab_v3_snapshot
    ts=pd.date_range('2026-10-07T13:00Z',periods=5,freq='min')
    raw=pd.DataFrame(dict(ts_event=ts))
    formed=int(ts[0].value//1000000)-86400000
    row=dict(candidate_id='C',dir=side,epoch=0,s=1,entry_ms=int(ts[4].value//1000000),entry=100.,
             fvg_lo=99.,fvg_hi=101.,brk=2,source_event={prefix+'_price':100.,'level_formed_ms':formed})
    base=SimpleNamespace(dol_resources=lambda r:(None,[dict(epoch=0,start=0,end=5)],{}),tick=lambda x:x,
                         tag_setup_dol=lambda *a,**k:SimpleNamespace(to_dict=lambda:dict(dol_status='UNKNOWN')))
    order=dict(candidate_id='C');short=SimpleNamespace(ssl_ledger=lambda *a:{})
    ab_v3_snapshot.attach(raw,[row],[order],base,short)
    meta=order['v3_snapshot']['boundary_context']
    assert meta==dict(setup_ms=int(ts[1].value//1000000),context_start_ms=int(ts[0].value//1000000),
                      source_price=100.,source_formed_ms=formed,fvg_lo=99.,fvg_hi=101.)
    assert row['brk']==2 and row['entry']==100.
