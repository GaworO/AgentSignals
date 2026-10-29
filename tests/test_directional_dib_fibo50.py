import copy,json
from pathlib import Path
import pandas as pd
import pytest
import ab_directional_engine as engine
import ab_directional_entry_rules as rules
import directional_dib_policy as policy
import directional_setup_review as setup_review

CASES=json.loads((Path(__file__).parent/'fixtures/dib_fibo50_recorded.json').read_text())

def frame(case):
 d=pd.DataFrame(case['bars']);d['ts_event']=pd.to_datetime(d.ts_ms,unit='ms',utc=True);return d

@pytest.fixture
def on(monkeypatch):
 for k,v in {policy.FLAG:'true','AB_AI_ENABLED':'true','AB_AI_TRIGGER':'HYBRID','V3_AI_REVIEW_REQUIRED':'true'}.items():monkeypatch.setenv(k,v)

@pytest.mark.parametrize('case',CASES,ids=lambda x:str(x['id']))
def test_all_recorded_anchors_reproduce_paired_replay(case,on):
 q=case['candidate'];d=frame(case);e=policy.derive(d,q,q['dir']);g=e['geometry']
 for k in ['entry_price','stop_price','target_price','activation_ms','expiry_ms','direction']:assert g[k]==case['expected'][k]
 assert e['original_stop_price']==case['expected']['stop_price']
 # Future data can settle an outcome but cannot change the entry.
 future=d.iloc[-1:].copy();future['ts_event']+=pd.Timedelta(minutes=5);future[['open','high','low','close']]=[1,1000000,.25,2]
 assert policy.derive(pd.concat([d,future],ignore_index=True),q,q['dir'])==e

def live_row(case):
 c,orders,f=engine.build_manifests(frame(case),[case['candidate']],case['candidate']['dir']);assert len(orders)==1
 o=orders[0];row=dict(o,activation_ms=int(o['activation_timestamp'].value//1000000),expiry_ms=int(o['expiry_timestamp'].value//1000000),stop_price=o['structural_sl_price'],target_price=o['policy_B_target'])
 row['payload_json']=json.dumps({'setup_category':o['setup_category'],'entry_rules':o['entry_rules']})
 return row

@pytest.mark.parametrize('case',[CASES[13],CASES[33]],ids=['SHORT','LONG'])
def test_geometry_computed_before_mandatory_exact_ai_packet(case,on):
 row=live_row(case);assert rules.dispatch_blocker(row) is None
 packet=setup_review.describe(row,'MNQZ2026')
 for k in ['entry_price','stop_price','target_price','expiry_ms']:assert packet[k]==case['expected'][k]
 assert packet['entry_rules']['dib_entry']['geometry']['fraction']==.5
 assert packet['order_id']==row['order_id']

@pytest.mark.parametrize('field',['entry_price','stop_price','target_price','expiry_ms','direction','instrument_id'])
def test_changed_geometry_rejected_at_dispatch(field,on):
 row=live_row(CASES[13]);row[field]='LONG' if field=='direction' else row[field]+1
 assert rules.dispatch_blocker(row) is not None

@pytest.mark.parametrize('key,value',[('AB_AI_ENABLED','false'),('V3_AI_REVIEW_REQUIRED','false'),('AB_AI_TRIGGER','PERIODIC')])
def test_no_periodic_or_optional_ai_fallback(key,value,on,monkeypatch):
 row=live_row(CASES[13]);monkeypatch.setenv(key,value)
 assert rules.dispatch_blocker(row)=='v3_entry:DIB_EXACT_AI_REVIEW_REQUIRED'

def test_default_disabled_and_operator_disable_invalidates_pending(on,monkeypatch):
 row=live_row(CASES[13]);monkeypatch.delenv(policy.FLAG)
 assert rules.dispatch_blocker(row)=='v3_entry:V3_DIB_DISABLED'
 assert engine.build_manifests(frame(CASES[13]),[CASES[13]['candidate']],'SHORT')[1]==[]

def test_old_evidence_does_not_gain_permission(on):
 row=live_row(CASES[13]);p=json.loads(row['payload_json']);p['entry_rules'].pop('dib_entry');row['payload_json']=json.dumps(p)
 assert rules.dispatch_blocker(row)=='v3_entry:DIB_PRICE_EVIDENCE_INVALID'

def test_missing_or_future_anchor_does_not_revert_to_62(on):
 case=copy.deepcopy(CASES[13]);case['candidate'].pop('origin_bar')
 assert engine.build_manifests(frame(case),[case['candidate']],'SHORT')[1]==[]
 case=copy.deepcopy(CASES[13]);case['candidate']['entry_ms']=case['candidate']['bos_ms']
 assert engine.build_manifests(frame(case),[case['candidate']],'SHORT')[1]==[]

def test_other_bos_filter_still_applies(on):
 case=CASES[24];c,o,f=engine.build_manifests(frame(case),[case['candidate']],'LONG')
 assert not o and c[0]['rejection_reason']=='V3_BOS_BODY_BELOW_50PCT'

def test_non_dib_geometry_unchanged_when_opt_in_enabled(on,monkeypatch):
 case=copy.deepcopy(CASES[13]);case['candidate']['cat']='PDH'
 a=engine.build_manifests(frame(case),[case['candidate']],'SHORT')
 monkeypatch.setenv(policy.FLAG,'false');b=engine.build_manifests(frame(case),[case['candidate']],'SHORT')
 assert a[1]==b[1]

def test_policy_invalid_setting_fails_closed(monkeypatch):
 monkeypatch.setenv(policy.FLAG,'TRUE');assert not policy.enabled()

def test_confirmation_index_not_used_as_extreme(on):
 case=copy.deepcopy(CASES[13]);a=policy.derive(frame(case),case['candidate'],'SHORT')
 case['candidate']['hh_bar']=999999
 assert policy.derive(frame(case),case['candidate'],'SHORT')==a
