"""Offline acceptance demonstration; injected broker only, no live clients.

Uses a recorded setup. All approvals/quotes/allowances/fills are MOCK data.
The user-confirmed minimum is 2R gross. Nonzero costs remain in the evidence
and simulated accounting; this is not live or provider-paper acceptance.
"""
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import execution_validation as gate
import directional_ai_review
import builder_crosstrade as broker
from test_builder_crosstrade import NativeTests
from test_execution_readiness import evidence


def run():
    recorded=json.loads((ROOT/'tests/fixtures/phase2b_recorded_setup.json').read_text())['setup']
    f=NativeTests();f.setUp()
    try:
        with f.api.connect() as db:db.execute('DELETE FROM orders')
        f.now=recorded['activation_ms']/1000+30
        f.order.update({k:recorded[k] for k in ('order_id','direction','entry_price','stop_price','target_price','risk_points','activation_ms','expiry_ms','contract')})
        f.fixture_round_trip=1.90;f.prepare();f.sync.poll()
        a=evidence();a[0]=copy.deepcopy(f.order);a[-1]=int(f.now*1000)
        a[1].update(bid=f.order['entry_price']-.25,ask=f.order['entry_price'],as_of_ms=a[-1])
        for item in a[2:4]:item.update(order_id=f.order['order_id'],checked_ms=a[-1])
        a[4].update(order_id=f.order['order_id'],setup=recorded,available_at=f.now-1,
                    expires_at=recorded['activation_ms']/1000+120,review_id='MOCK_PHASE2B_NO_API_CALL')
        a[5]['max_signal_age_ms']=120000
        a[5]['costs']['round_trip_usd']=1.90
        a[5]['costs']['validation_reference']='MOCK_MATCHED_TO_RECORDED_1_90_RT'
        real_reason=gate.evaluate(*a)
        assert real_reason is None
        variants={}
        for label in ('WAIT','expired_setup','adverse_price','insufficient_rr','stale_quote','risk_exceeded','changed_structure'):
            args=copy.deepcopy(a)
            if label=='WAIT':args[4]['state']='WAIT'
            elif label=='expired_setup':args[0]['expiry_ms']=args[-1]
            elif label=='adverse_price':args[1]['bid']+=.25;args[1]['ask']+=.25
            elif label=='insufficient_rr':args[0]['target_price']-=.25
            elif label=='stale_quote':args[1]['as_of_ms']-=2000
            elif label=='risk_exceeded':args[3].update(allowed=False,reason='SIMULATED_DAILY_LOSS')
            else:args[2].update(valid=False,reason='SIMULATED_NEW_STRUCTURE')
            reason=gate.evaluate(*args);assert reason
            variants[label]=dict(input=args,output='BLOCK',reason=reason,orders=0)
        stages=[]
        def stage(name,inputs,output,reason):
            stages.append(dict(stage=name,at_ms=int(f.now*1000),input=inputs,output=output,reason=reason,kind='SIMULATED'))
        stage('Exact setup',recorded,'BOUND','Original production geometry unchanged')
        stage('AI Review',a[4],'ALLOW','MOCK approval. No model call.')
        stage('Post-AI market / risk / quote validation',dict(thesis=a[2],quote=a[1],risk=a[3],policy=a[5]),
              'ALLOW' if gate.evaluate(*a) is None else 'BLOCK','2R gross, $1.90 estimated RT cost; MOCK evidence, NOT production approval')
        f.final_gate.side_effect=lambda order,review:gate.evaluate(order,a[1],a[2],a[3],review,a[5],int(f.now*1000))
        with patch.object(directional_ai_review,'check',return_value=(None,a[4])):
            ack=f.submit()
        assert ack['sent'] and f.local()['position_json'] is None
        stage('LIMIT → CrossTrade ACK',f.posts[-1][1],ack,'ACK is not fill; one durable intent')
        f.accept(f.order,dict(state='SENT',quantity=2,broker=ack));f.open()
        obs=json.loads(f.t.row(f.order['order_id'])['observation_json'])
        assert obs['protection_verified']
        stage('Fill → native SL/TP',dict(lifecycles=f.life,fills=f.sync.snapshot()['fills']),obs,'Owned fills and working OCO bracket confirmed in simulator')
        resumed=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
        resumed.reconcile_one(resumed.row(f.order['order_id']),f.sync.snapshot())
        assert len(f.posts)==1
        stage('Application restart',dict(durable_intent=resumed.row(f.order['order_id'])['state']), 'OPEN','No duplicate POST')
        snapshot,net=f.complete(winning=False)
        resumed.reconcile_one(resumed.row(f.order['order_id']),snapshot)
        assert resumed.resolution(f.order['order_id'])['outcome']=='closed_pnl_pending'
        stage('Stop fill → flat',dict(fills=snapshot['fills'],positions=snapshot['positions']),resumed.resolution(f.order['order_id']),'Flat verified; net still unconfirmed')
        resumed.reconcile_costs(snapshot)
        assert resumed.resolution(f.order['order_id'])['net']==net
        stage('Accounting',dict(history=f.history,cash=snapshot['account']['balance']),resumed.resolution(f.order['order_id']),
              'MOCK $1.90 RT/contract matched to fills and cash; fees remain revisable')
        # A separately reset deterministic broker fixture proves rejection.
        result=dict(recorded_setup=recorded,actual_cost_gate=dict(reason=real_reason,allowed=real_reason is None,rr=gate.reward_risk(recorded,a[5])),
                    simulated_protocol=stages,blocked_variants=variants,
                    simulated_net_usd=net,paid_calls=0,real_orders=0,simulated_entry_posts=len(f.posts))
        return result
    finally:f.doCleanups()


def rejected_demo():
    f=NativeTests();f.setUp()
    try:
        ack=f.submit();f.life[11]['order']['ordStatus']='Rejected'
        for oid in (12,13):f.life[oid]['order']['ordStatus']='Canceled'
        for _ in range(2):
            f.now+=16;f.t.reconcile_one(f.t.row('order-one'),f.snapshot())
        assert f.t.resolution('order-one')['outcome']=='no_fill'
        return dict(kind='SIMULATED',input=dict(ack=ack,lifecycles=f.life),
                    output=f.t.resolution('order-one'),reason='Broker rejection supersedes ACK; no fill, no retry',
                    real_orders=0,simulated_entry_posts=len(f.posts))
    finally:f.doCleanups()


if __name__=='__main__':
    output=Path(sys.argv[1]);result=run();result['broker_rejection']=rejected_demo()
    output.write_text(json.dumps(result,indent=2,ensure_ascii=False))
    print('Saved offline demonstration. Paid AI calls: 0. Real broker orders: 0.')
