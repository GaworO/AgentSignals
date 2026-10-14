"""Synthetic wiring tests; no API calls, execution or performance claims."""
import copy
import json
import unittest
from unittest.mock import patch
from datetime import datetime
from ai_entry_gate import evaluate,apply,TTL_SECONDS,NY
from context_v3 import unknown_decision
from worker import build_v3_packet,Bar


class GateTests(unittest.TestCase):
    def setUp(self):
        self.t=int(datetime(2026,10,8,10,tzinfo=NY).timestamp())
        self.d=dict(context=dict(bias='long',decision='candidate',missing_context=[],claims={'structure_intact':{'value':True}}),analysis_symbol='MNQ',execution_symbol='MNQ',entry_mode='immediate_after_confirmation',invalidation={'evidence_id':'low','field':'price'},initial_stop={'price':98},management={'targets':[{'price':120}]},rationale='Synthetic permitted thesis')
        self.p=dict(packet_id='fixture',as_of=self.t,evidence={'low':dict(symbol='MNQ',kind='level',price=99,available_at=self.t)})
        self.r=dict(id='fixture-review',status='validated',cutoff=self.t,started=self.t+2,finished=self.t+25,packet_id='fixture',snapshot=json.dumps(dict(packet=self.p,frozen_at=self.t+1,processed_at=self.t+1)),decision=json.dumps(self.d))
        self.m={'MNQ':[dict(time=self.t,open=105,high=110,low=103,close=108,received_at=self.t+61),dict(time=self.t+60,open=108,high=112,low=104,close=111,received_at=self.t+121)]}
        self.validator=patch('ai_entry_gate.validate_decision',return_value=dict(context_review_state='CONDITIONS_MET',missing_plan_fields=[]))
        self.validator.start();self.addCleanup(self.validator.stop)
    def run_gate(self,**kw):
        return evaluate(kw.get('row',self.r),kw.get('markets',self.m),cutoff=kw.get('cutoff',self.t+120),now=kw.get('now',self.t+121))
    def update_decision(self,**changes):
        self.d.update(changes);self.r['decision']=json.dumps(self.d)
    def test_available_after_answer_but_not_retroactive(self):
        self.assertEqual(self.run_gate()['state'],'PERMITTED')
        self.assertEqual(self.run_gate(cutoff=self.t,now=self.t+26)['reasons'],['AI_CONTEXT_NOT_AVAILABLE_AT_BAR_CLOSE'])
        self.r['finished']=self.t+122
        self.assertEqual(self.run_gate()['state'],'BLOCKED')
    def test_expiry_uses_market_cutoff_not_response_receipt(self):
        self.r['finished']=self.t+899
        self.assertEqual(self.run_gate(cutoff=self.t+900,now=self.t+900)['reasons'],['AI_CONTEXT_EXPIRED'])
    def test_touch_then_recovery_stays_invalid_and_missing_minute_blocks(self):
        self.m['MNQ'][0]['low']=99
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_PRICE_INVALIDATED'])
        self.assertEqual(self.run_gate()['invalidated_at'],self.t+60)
        self.m['MNQ'].pop(0)
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_MONITORING_GAP'])
    def test_late_received_bar_and_future_poison_do_not_unlock(self):
        self.m['MNQ'][0]['received_at']=self.t+500
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_MONITORING_GAP'])
        self.m['MNQ'][0]['received_at']=self.t+61
        baseline=self.run_gate()
        self.m['MNQ'].append(dict(time=self.t+120,high=1e9,low=0,received_at=self.t+180))
        self.assertEqual(self.run_gate(),baseline)
    def test_short_direction_and_targets(self):
        self.d['context']['bias']='short';self.d['initial_stop']['price']=116
        self.d['management']['targets'][0]['price']=90
        self.p['evidence']['low']['price']=115
        self.r['snapshot']=json.dumps(dict(packet=self.p,frozen_at=self.t+1,processed_at=self.t+1))
        self.update_decision()
        self.assertEqual(self.run_gate()['state'],'PERMITTED')
        self.m['MNQ'][0]['high']=115
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_PRICE_INVALIDATED'])
        self.m['MNQ'][0]['high']=110;self.m['MNQ'][1]['low']=90
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_OBJECTIVE_ALREADY_REACHED'])
    def test_long_target_already_hit(self):
        self.m['MNQ'][0]['high']=120
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_OBJECTIVE_ALREADY_REACHED'])
    def test_newest_wait_failure_and_unknown_never_unlock(self):
        for status in ('running','failed','rejected','interrupted'):
            self.r['status']=status;self.assertEqual(self.run_gate()['state'],'BLOCKED')
        self.r['status']='validated'
        for decision in ('wait','abstain'):
            self.d['context']['decision']=decision;self.update_decision()
            self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_'+decision.upper()])
    def test_forged_or_previous_session_or_holiday(self):
        self.r['packet_id']='forged';self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_EVIDENCE_MISMATCH'])
        self.assertEqual(self.run_gate(now=self.t+86400)['reasons'],['AI_CONTEXT_PREVIOUS_SESSION'])
        self.assertEqual(self.run_gate(now=int(datetime(2026,10,12,10,tzinfo=NY).timestamp()))['state'],'BLOCKED')
    def test_gate_removes_blocked_plan_and_caps_permitted_expiry(self):
        def result(direction='long'):
            return dict(state='PLAN_READY',reasons=[],selected={'selection':{'direction':direction,'context_valid_until':self.t+2000}},compiled={'plan':{'context_valid_until':self.t+2000}})
        g=self.run_gate();r=apply(result(),g)
        self.assertEqual(r['compiled']['plan']['context_valid_until'],self.t+TTL_SECONDS)
        self.assertEqual(r['compiled']['plan']['ai_run_id'],self.r['id'])
        r=apply(result('short'),g)
        self.assertEqual(r['state'],'AI_CONTEXT_BLOCKED');self.assertIsNone(r['compiled']['plan'])
    def test_real_validator_accepts_abstention_as_block_and_rejects_forgery(self):
        self.validator.stop()
        bs=[Bar(t,100,102,99,101) for t in range(self.t-16*3600,self.t,60)]
        p=build_v3_packet({'ES':bs,'MNQ':bs},self.t)
        self.r.update(packet_id=p['packet_id'],snapshot=json.dumps(dict(packet=p,frozen_at=self.t+1,processed_at=self.t+1)),decision=json.dumps(unknown_decision(p)))
        self.assertEqual(self.run_gate()['reasons'],['AI_CONTEXT_ABSTAIN'])
        p['as_of']-=60;self.r['snapshot']=json.dumps(dict(packet=p,frozen_at=self.t+1,processed_at=self.t+1))
        self.assertEqual(self.run_gate()['state'],'BLOCKED')

if __name__=='__main__':unittest.main()
