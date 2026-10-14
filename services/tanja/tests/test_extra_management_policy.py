import unittest
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from management_policy import es_session_partial_proposal,add_on_proposal,local_partial_proposal
import test_selection_observer as observer_tests

class ESPartialTests(unittest.TestCase):
    def setUp(self):
        self.start=int(datetime(2026,10,8,9,30,tzinfo=ZoneInfo('America/New_York')).timestamp())
        self.bars=[dict(time=self.start+i*60,open=100,high=101,low=99,close=100) for i in range(5)]
        self.bars[-1]['high']=102
        self.args=dict(position_id='synthetic',direction='long',original_quantity=10,remaining_quantity=10,opened_at=self.start+60,
            es_bars=self.bars,now=self.start+300,first_es_partial_done=False,partial_pending=False)
    def run_proposal(self,**kw):return es_session_partial_proposal(**dict(self.args,**kw))
    def test_new_high_and_equal_high_and_short_mirror(self):
        r=self.run_proposal();self.assertEqual(r['quantity'],2);self.assertEqual(r['trigger'],'ES_NEW_SESSION_HIGH');self.assertFalse(r['is_order'])
        self.bars[-1]['high']=101;self.assertEqual(self.run_proposal()['reason'],'NO_NEW_ES_SESSION_EXTREME')
        self.bars[-1]['low']=98
        r=self.run_proposal(direction='short');self.assertEqual(r['trigger'],'ES_NEW_SESSION_LOW');self.assertEqual(r['quantity'],2)
    def test_history_gap_future_receipt_and_before_entry_do_not_trigger(self):
        self.assertEqual(self.run_proposal(es_bars=self.bars[1:])['action'],'NONE')
        self.assertEqual(self.run_proposal(now=self.start+299)['action'],'NONE')
        self.assertEqual(self.run_proposal(opened_at=self.start+241)['reason'],'ES_EVENT_NOT_FULLY_AFTER_ENTRY')
        self.assertEqual(self.run_proposal(now=self.start+316)['reason'],'ES_EVENT_STALE')
        self.bars[-1]['received_at']=self.start+320
        self.assertEqual(self.run_proposal()['action'],'NONE')
    def test_flags_original_size_runner_and_stable_id(self):
        a=self.run_proposal();b=self.run_proposal(now=self.start+301);self.assertEqual(a['intent_id'],b['intent_id'])
        for kw in ({'first_es_partial_done':True},{'partial_pending':True},{'remaining_quantity':2},{'original_quantity':4,'remaining_quantity':4}):
            self.assertEqual(self.run_proposal(**kw)['action'],'NONE')
        self.assertEqual(self.run_proposal(remaining_quantity=12)['quantity'],2) # add does not increase partial base
    def test_future_extreme_cannot_rewrite_closed_event(self):
        a=self.run_proposal();future=dict(self.bars[-1],time=self.start+300,high=900)
        self.assertEqual(a,self.run_proposal(es_bars=self.bars+[future]))

class AddTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real selector + validator + observer, not a forged approval.
        self.case=observer_tests.SelectionObserverTests('test_real_validated_thesis_permits_matching_plan_and_caps_expiry')
        self.case.setUp();self.addCleanup(self.case.doCleanups);self.case.add_review();self.case.o.process_one(self.case.t+1)
        self.r=self.case.o.audit(self.case.o.revision,self.case.t)
        self.args=dict(position_id='original-confirmed-position',direction='long',original_quantity=10,remaining_quantity=10,
            opened_at=self.case.t-600,current_stop=100,original_risk_budget_usd=250,committed_risk_usd=100,
            decision=self.r,now=self.case.t+1,additions_done=0,add_pending=False)
    def proposal(self,**kw):return add_on_proposal(**dict(self.args,**kw))
    def test_twenty_percent_shared_risk_and_one_add(self):
        r=self.proposal();self.assertEqual(r['action'],'REVIEW_ADD_PROPOSAL');self.assertEqual(r['quantity'],2);self.assertFalse(r['is_order'])
        self.assertLessEqual(r['planned_add_risk'],150)
        for kw in ({'additions_done':1},{'add_pending':True},{'committed_risk_usd':249},{'original_quantity':4,'remaining_quantity':4}):
            self.assertEqual(self.proposal(**kw)['action'],'NONE')
        r=self.proposal(committed_risk_usd=210);self.assertEqual(r['quantity'],1)
    def test_fresh_setup_same_direction_and_all_gates_required(self):
        for kw in ({'direction':'short'},{'opened_at':self.case.t},{'now':self.case.t+16},{'position_id':self.r['compiled']['plan']['plan_id']}):
            self.assertEqual(self.proposal(**kw)['action'],'NONE')
        for key,field,value in [('scheduled_news','state','BLOCKED'),('ai_context','state','BLOCKED'),('account_guard','state','BLOCKED')]:
            d=deepcopy(self.r);d[key][field]=value;self.assertEqual(self.proposal(decision=d)['action'],'NONE')
        d=deepcopy(self.r);d['selected']['selection']['timeframe_minutes']=1
        self.assertEqual(self.proposal(decision=d)['reason'],'M5_CLOSE_REQUIRED')
    def test_contract_cap_and_current_stop_not_new_initial_stop(self):
        d=deepcopy(self.r);d['account_guard']['max_contracts']=1
        r=self.proposal(decision=d,current_stop=110);self.assertEqual(r['quantity'],1);self.assertEqual(r['shared_stop'],110)
        self.assertEqual(self.proposal(current_stop=120)['action'],'NONE')
    def test_short_add_from_real_short_gated_plan(self):
        import json
        from test_short_selector import short_fixture
        markets,_=short_fixture()
        with self.case.s.connect() as db:
            db.execute('DELETE FROM auto_selections');db.execute('DELETE FROM ai_runs')
            for symbol,rows in markets.items():
                table='collection_bars' if symbol=='NQ' else 'bars'
                for b in rows:
                    payload=dict(symbol=symbol,bar_open_ms=b['time']*1000,bar_close_ms=(b['time']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                    db.execute(f'UPDATE {table} SET payload=? WHERE symbol=? AND start=?',(json.dumps(payload),symbol,b['time']))
        self.case.add_review('short');self.case.o.process_one(self.case.t+1)
        d=self.case.o.audit(self.case.o.revision,self.case.t)
        r=self.proposal(decision=d,direction='short',current_stop=900)
        self.assertEqual(r['action'],'REVIEW_ADD_PROPOSAL');self.assertEqual(r['quantity'],2)
        self.assertLess(r['reference_entry'],r['shared_stop'])

    def test_local_partial_still_uses_original_size_after_add(self):
        now=self.case.t+1
        r=local_partial_proposal(position_id='p',direction='long',original_quantity=5,remaining_quantity=6,entry=100,opened_at=now-60,
            objective=dict(price=105,available_at=now-120,source='selected'),final_target=120,now=now,market_price=105,price_known_at=now,
            first_partial_done=False,partial_pending=False)
        self.assertEqual(r['quantity'],1);self.assertEqual(r['remaining_after_fill'],5)

if __name__=='__main__':unittest.main()
