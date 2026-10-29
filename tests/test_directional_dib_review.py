"""Exact HYBRID worker for the opt-in DIB geometry. Provider and broker mocked."""
import copy,json
import pytest
import ab_directional_entry_rules as rules
import directional_dib_policy as policy
import directional_ai_review as gate
import directional_recovery_runtime as runtime
import directional_recovery_plans as plans
from test_directional_setup_review import service,order
from test_directional_hybrid import h,mocked_response
from test_directional_recovery_wick import market
from test_directional_recovery_runtime import rig

def dib_order(h,monkeypatch):
 for k,v in {policy.FLAG:'true','AB_AI_ENABLED':'true','AB_AI_TRIGGER':'HYBRID','V3_AI_REVIEW_REQUIRED':'true'}.items():monkeypatch.setenv(k,v)
 o=order(h);p=json.loads(o['payload_json']);p['setup_category']='PDH+DIB'
 t=o['activation_ms']//1000
 proof=dict(version=policy.VERSION,source='ORIGINAL_DIRECTIONAL_SCANNER',direction='LONG',instrument_id=1,
  activation_ms=o['activation_ms'],expiry_ms=o['expiry_ms'],original_raw_entry=94.44,original_stop_price=90.,
  origin=dict(time=t-180,available_at=t-120,instrument_id=1,open=82.,high=90.,low=80.,close=88.),
  extreme=dict(time=t-120,available_at=t-60,instrument_id=1,open=90.,high=118.,low=89.,close=110.))
 proof['geometry']=policy.calculate(proof);proof['fingerprint']=policy.digest(proof)
 e=p['entry_rules'];e['category']='PDH+DIB';e['version']=rules.DIB_RULE_VERSION;e['dib_entry']=proof;o['payload_json']=json.dumps(p)
 assert rules.dispatch_blocker(o) is None
 return o

@pytest.mark.parametrize('decision',['ALLOW','WAIT','timeout','expired','changed_price'])
def test_dib_exact_hybrid_response_and_failure_handling(h,monkeypatch,decision):
 o=dib_order(h,monkeypatch)
 def transport(payload):
  p=json.loads(payload['input'][1]['content'])
  assert p['setup']['entry_price']==100 and p['setup']['entry_rules']['dib_entry']['geometry']['fraction']==.5
  if decision=='timeout':raise TimeoutError()
  raw=mocked_response(h,payload);d=json.loads(raw['output'][0]['content'][0]['text'])
  if decision=='WAIT':d['long_scenario']['permission']='WAIT'
  if decision=='changed_price':d['approved_geometry']['entry_price']=95.5
  if decision=='expired':h.now+=121
  raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
 h.support.transport=transport;assert h.support.queue_setup(o)['state']=='queued';assert h.support.process_one()
 reason,annotation=gate.check('LONG',o['order_id'])
 assert (reason is None)==(decision=='ALLOW')
 with h.support.connect() as db:
  r=db.execute('SELECT * FROM reviews').fetchone();assert r['request']
  if decision=='ALLOW':assert r['response_text'] and r['review_kind']=='SETUP'

def test_original_62_geometry_cannot_enter_new_review(h,monkeypatch):
 o=dib_order(h,monkeypatch);o['entry_price']=95.5;o['target_price']=106
 assert h.support.queue_setup(o)['state']=='INELIGIBLE';assert not h.calls

def test_disabling_dib_prevents_new_recovery_spend_or_submission(rig,monkeypatch):
 o=copy.deepcopy(rig.original);o['dib_entry_policy']=policy.VERSION
 with rig.connect() as db:db.execute('UPDATE nofill_opportunities SET original=?',(json.dumps(o),))
 monkeypatch.delenv(policy.FLAG,raising=False)
 assert not rig.run();assert not rig.calls and not rig.posts

def test_dib_marker_survives_immutable_recovery_binding(market):
 o,p,s=market;o['dib_entry_policy']=policy.VERSION
 assert plans.original_view(o)['dib_entry_policy']==policy.VERSION
 a=plans.digest(plans.original_view(o));o['dib_entry_policy']='changed'
 assert plans.digest(plans.original_view(o))!=a
