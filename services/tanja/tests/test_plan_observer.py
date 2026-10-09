import base64,io,json,tempfile,unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from app import App
from plan_observer import PlanObserver,assess
from store import Store
from ai_review import AIReview
from worker import build_v3_packet,Bar
from context_v3 import unknown_decision

class TestPlanObserver(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.cutoff=1779807600
  bs=[Bar(t,100,102,99,101) for t in range(cls.cutoff-16*3600,cls.cutoff,60)]
  cls.packet=build_v3_packet({'ES':bs,'MNQ':bs},cls.cutoff)
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);AIReview(self.store,{})
  self.observer=PlanObserver(self.store)
 def tearDown(self):self.tmp.cleanup()
 def fixture(self,id='review',**updates):
  p=self.packet
  row=dict(id=id,packet_id=p['packet_id'],cutoff=self.cutoff,started=self.cutoff+2,finished=self.cutoff+5,status='validated',
   snapshot=json.dumps(dict(packet=p,frozen_at=self.cutoff+1,processed_at=self.cutoff+1)),decision=json.dumps(unknown_decision(p)))
  row.update(updates);return row
 def insert(self,row):
  keys=list(row)
  with self.store.connect() as db:db.execute('INSERT INTO ai_runs('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])
 def test_real_abstention_validation_and_restart(self):
  self.insert(self.fixture());self.assertTrue(self.observer.process_one(self.cutoff+10))
  row=self.observer.state()['records'][0];self.assertEqual(row['state'],'ABSTAINED');self.assertFalse(row['executable'])
  other=PlanObserver(self.store);self.assertFalse(other.process_one(self.cutoff+11));self.assertEqual(len(other.state()['records']),1)
 def test_no_observation_before_ai_answer_arrives(self):
  self.insert(self.fixture());self.assertFalse(self.observer.process_one(self.cutoff+4));self.assertTrue(self.observer.process_one(self.cutoff+5))
 def test_forged_packet_and_time_rejected(self):
  row=self.fixture(packet_id='forged');self.assertEqual(assess(row)['state'],'AUDIT_REJECTED')
  row=self.fixture(finished=self.cutoff-1);self.assertEqual(assess(row)['state'],'AUDIT_REJECTED')
 def test_failed_and_interrupted_reviews_never_unlock(self):
  self.insert(self.fixture(status='interrupted',finished=None));self.observer.process_one(self.cutoff+10)
  row=self.observer.state()['records'][0];self.assertEqual(row['state'],'AI_NOT_VALIDATED');self.assertIsNone(row['available_at'])
 def test_validated_without_finish_rejected(self):
  self.insert(self.fixture(finished=None));self.observer.process_one(self.cutoff+10)
  self.assertEqual(self.observer.state()['records'][0]['state'],'AUDIT_REJECTED')
 def test_concurrent_processing_records_once(self):
  self.insert(self.fixture())
  with ThreadPoolExecutor(max_workers=2) as pool:done=list(pool.map(lambda _:self.observer.process_one(self.cutoff+10),range(2)))
  self.assertEqual(sum(done),1);self.assertEqual(len(self.observer.state()['records']),1)
 def candidate(self):
  row=self.fixture();d=json.loads(row['decision']);d['context'].update(decision='candidate',bias='long',selected_trigger_id='fixture-trigger')
  d.update(execution_symbol='MNQ',entry_mode='immediate_after_confirmation');row['decision']=json.dumps(d)
  return row
 def test_complete_geometry_is_never_strategy_or_order_approval(self):
  review=dict(measurements={},missing_plan_fields=[],context_review_state='CONDITIONS_MET')
  with patch('plan_observer.validate_decision',return_value=review):result=assess(self.candidate())
  self.assertEqual(result['state'],'RULE_REVIEW_REQUIRED');self.assertFalse(result['executable']);self.assertFalse(result['simulated_entry']);self.assertEqual(result['broker_orders_sent'],0)
 def test_incomplete_and_retracement_states(self):
  review=dict(measurements={},missing_plan_fields=['initial_stop'],context_review_state='CONDITIONS_MET')
  with patch('plan_observer.validate_decision',return_value=review):
   self.assertEqual(assess(self.candidate())['state'],'PLAN_INCOMPLETE')
   row=self.candidate();d=json.loads(row['decision']);d['entry_mode']='retracement';row['decision']=json.dumps(d)
   self.assertEqual(assess(row)['state'],'UNSUPPORTED_ENTRY_MODE')
 def test_authenticated_endpoint_read_only(self):
  cfg=dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='x'*40,TANJA_DASHBOARD_PASSWORD='test_password_1234')
  app=App(cfg)
  for method,auth,expected in [('GET',False,'401'),('GET',True,'200'),('POST',True,'405')]:
   env=dict(PATH_INFO='/api/plans',REQUEST_METHOD=method);env['wsgi.input']=io.BytesIO()
   if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test_password_1234').decode()
   statuses=[];body=b''.join(app(env,lambda s,h:statuses.append(s)))
   self.assertTrue(statuses[0].startswith(expected))
   if expected=='200':self.assertFalse(json.loads(body)['orders_enabled'])

if __name__=='__main__':unittest.main()
