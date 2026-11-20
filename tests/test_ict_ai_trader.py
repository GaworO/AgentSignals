import base64,json
from unittest.mock import patch
import pytest
from flask import Flask
import ict_ai_trader
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.tests.test_observer import FakeKnowledge,bar
from services.tanja.ict_agent import reasoning as r

@pytest.fixture
def store(tmp_path):
 s=Store(tmp_path/'observer.sqlite3',{'test':True});Observer(s,FakeKnowledge()).push([bar(symbol=x) for x in ('NQ','ES','MNQ')]);return s
def cfg(s):return {'DATA_DIR':str(s.path.parent),'TANJA_DASHBOARD_PASSWORD':'fixture-password','ICT_AI_REPLAY_DB':str(s.path),'ICT_AI_MODE':'OFF'}
def auth():return {'Authorization':'Basic '+base64.b64encode(b'tanja:fixture-password').decode()}
def packet(s):
 p=r.prepare(s,s.snapshot()['id']);p['source_records']=[{'id':'fixture-rule'}];return p
def answer(p):
 return dict(snapshot_id=p['snapshot']['id'],as_of=p['snapshot']['as_of'],decision='NO_TRADE',preferred_hypothesis_id=None,selected_rule_ids=[],
 assessments=[dict(hypothesis_id=i,stance='UNRESOLVED',supporting_market_ids=[],opposing_market_ids=[],supporting_rule_ids=[],opposing_rule_ids=[],missing=['Confirmation'],invalidation='Unknown',explanation='Insufficient evidence',entry_logic='Needs confirmation',next_event='Wait',lifecycle='WATCHING') for i in p['hypotheses']],claims=[],missing_conditions=['Confirmation'],next_observable_event='Wait for a closed candle',explanation='Insufficient confirmation',proposal=None,changes=dict(previous_snapshot_id=None,market_ids=[],interpretation='Initial review',thesis='NO_PRIOR'))
def paid(s):
 at=s.snapshot()['as_of'];return dict(ICT_AI_PAID_APPROVED='true',ICT_AI_APPROVAL_ID='UNIT_TEST_ONLY',ICT_AI_MODEL='fixture-model',ICT_AI_PROVIDER='openai',ICT_AI_DAILY_CALLS='2',ICT_AI_DAILY_USD='10',ICT_AI_INPUT_USD_PER_MILLION_CEILING='2',ICT_AI_OUTPUT_USD_PER_MILLION_CEILING='10',ICT_AI_MAX_OUTPUT_TOKENS='2000',ICT_AI_MAX_INPUT_TOKENS_UPPER='350000',ICT_AI_TIMEOUT_SECONDS='5',ICT_AI_APPROVAL_EXPIRES_AT=str(at+1000))
def response(a):return dict(status='completed',output=[dict(content=[dict(type='output_text',text=json.dumps(a))])],usage={'input_tokens':100,'output_tokens':100})

def test_unified_auth_readonly_modes(store):
 app=Flask(__name__);ict_ai_trader.register(app,cfg(store));c=app.test_client();sid=store.snapshot()['id']
 for route in ('','/assets/app.js','/api/state','/api/snapshots/'+sid,'/api/health'):
  assert c.get('/ict-ai-trader'+route).status_code==401
  z=c.get('/ict-ai-trader'+route,headers=auth());assert z.status_code==(302 if not route else 200) and z.headers['Cache-Control']=='no-store'
 assert c.post('/ict-ai-trader/api/state',headers=auth()).status_code==405
 assert c.get('/ict-ai-trader/api/state?mode=live',headers=auth()).status_code==400
 assert c.get('/ict-ai-trader/api/state?mode=observe',headers=auth()).json['available'] is False
 assert c.get('/ict-ai-trader',headers=auth()).headers['Location']=='/ict-shadow?view=ictai'
 assert c.get('/ict-ai-trader/api/state',headers=auth()).json['freshness']=='HISTORICAL'
 assert 'ict_ai_trader_worker' not in app.extensions

def test_existing_shadow_coexists(store):
 from ict_shadow_cloud import register
 import dashboard
 app=Flask(__name__);dashboard.register(app);cloud=register(app,cfg(store),start=False);before={str(x) for x in app.url_map.iter_rules()}
 ict_ai_trader.register(app,cfg(store),start=False);assert before<={str(x) for x in app.url_map.iter_rules()}
 c=app.test_client();assert c.get('/ict-shadow').status_code==401
 assert c.get('/ict-shadow',headers=auth()).status_code==200
 assert c.get('/ict-shadow/state',headers=auth()).status_code==200
 assert 'AI ICT Trader' in c.get('/ict-shadow',headers=auth()).text and cloud._worker_thread is None

def test_no_auth_config_denies(tmp_path):
 app=Flask(__name__);ict_ai_trader.register(app,{'DATA_DIR':str(tmp_path)},start=False);assert app.test_client().get('/ict-ai-trader').status_code==401

def test_asof_and_input_validation(store):
 app=Flask(__name__);ict_ai_trader.register(app,cfg(store),start=False);c=app.test_client();s=store.snapshot();url='/ict-ai-trader/api/snapshots/'+s['id']+'/charts'
 Observer(store,FakeKnowledge()).push([bar(1777642260,symbol=x,high=105) for x in ('NQ','ES','MNQ')])
 charts=c.get(url,headers=auth()).json;assert all(b['available_at']<=s['as_of'] for rows in charts.values() for b in rows)
 for arg in ('?symbol=../../etc','?limit=999999'):assert c.get(url+arg,headers=auth()).status_code==400
 assert c.get('/ict-ai-trader/assets/agent.py',headers=auth()).status_code==404

def test_numeric_claims_invented_refs(store):
 p=packet(store);a=answer(p);e=next(iter(p['evidence']));field=next(k for k in ('high','low','price','upper') if k in p['evidence'][e]);v=p['evidence'][e][field]
 a['claims']=[dict(text='Observed level',facts=[dict(evidence_id=e,field=field,value=v)],source_rule_ids=['fixture-rule'])]
 assert r.validate(a,p,a['as_of'])['valid']
 a['claims'][0]['facts'][0]['value']=v+.25
 with pytest.raises(ValueError,match='NUMERICAL'):r.validate(a,p,a['as_of'])
 a['claims'][0]['facts'][0]['value']=v;a['explanation']='Entry at 99999'
 with pytest.raises(ValueError,match='NUMBERS_REQUIRE'):r.validate(a,p,a['as_of'])
 a['explanation']='Wait';a['selected_rule_ids']=['invented']
 with pytest.raises(ValueError,match='INVENTED_RULE'):r.validate(a,p,a['as_of'])

def test_competing_hypotheses_and_stale(store):
 p=packet(store);a=answer(p);assert r.validate(a,p,a['as_of'])['valid']
 with pytest.raises(ValueError,match='EXPIRED'):r.validate(a,p,a['as_of']+121)
 a['assessments'].pop()
 with pytest.raises(ValueError,match='ALL_HYPOTHESES'):r.validate(a,p,a['as_of'])

def test_paid_authorization_and_key(store):
 with pytest.raises(ValueError,match='NOT_AUTHORIZED'):r.review(store,store.snapshot()['id'],{},transport=lambda _:pytest.fail('paid call'))
 with pytest.raises(ValueError,match='DEDICATED'):r.review(store,store.snapshot()['id'],paid(store),clock=lambda:store.snapshot()['as_of'])

def test_real_transport_requires_shared_cap_and_verified_rates(store):
 c=dict(paid(store),ICT_AI_API_KEY='UNIT_TEST_NOT_A_REAL_KEY')
 with patch.object(r,'openai_transport',side_effect=AssertionError('must not call provider')):
  with pytest.raises(ValueError,match='SHARED_TOTAL_BUDGET'):r.review(store,store.snapshot()['id'],c,clock=lambda:store.snapshot()['as_of'])
  c.update(ICT_AI_TOTAL_BUDGET_DB=str(store.path.parent/'cap.sqlite3'),ICT_AI_TOTAL_USD='8')
  with pytest.raises(ValueError,match='UNVERIFIED_MODEL_OR_RATE'):r.review(store,store.snapshot()['id'],c,clock=lambda:store.snapshot()['as_of'])

def test_budget_restart_memory(store):
 p=packet(store);a=answer(p);at=a['as_of'];calls=[]
 def send(b):calls.append(b);return response(a)
 with patch.object(r,'prepare',return_value=p):
  out=r.review(store,a['snapshot_id'],paid(store),transport=send,clock=lambda:at)
  assert out['status']=='VALIDATED_ADVISORY' and out['usage']['input_tokens']==100 and out['orders_sent']==0
  assert out['origin']=='INJECTED_TEST_TRANSPORT'
  with pytest.raises(ValueError,match='ALREADY_RESERVED'):r.review(Store(store.path),a['snapshot_id'],paid(store),transport=send,clock=lambda:at)
 assert len(calls)==1
 with store.readonly() as d:assert d.execute('SELECT count(*) FROM reasoning_memory').fetchone()[0]==1

def test_timeout_not_retried(store):
 p=packet(store);sid=p['snapshot']['id'];at=p['snapshot']['as_of'];calls=[]
 def timeout(b):calls.append(1);raise TimeoutError()
 with patch.object(r,'prepare',return_value=p):
  out=r.review(store,sid,paid(store),transport=timeout,clock=lambda:at);assert out['status']=='TRANSPORT_OUTCOME_UNKNOWN'
  with pytest.raises(ValueError,match='ALREADY_RESERVED'):r.review(store,sid,paid(store),transport=timeout,clock=lambda:at)
 with store.readonly() as d:assert d.execute('SELECT actual_usd,state FROM reasoning_budget').fetchone()==(None,'UNKNOWN_RESERVED')
 assert len(calls)==1

def test_call_cap_variants(store):
 p=packet(store);at=p['snapshot']['as_of'];c=paid(store);c['ICT_AI_DAILY_CALLS']='1'
 with patch.object(r,'prepare',return_value=p):
  r.review(store,p['snapshot']['id'],c,transport=lambda _:response(answer(p)),clock=lambda:at)
  with pytest.raises(ValueError,match='DAILY_BUDGET'):r.review(store,p['snapshot']['id'],c,variant='B_LLM_NO_RAG',transport=lambda _:pytest.fail('budget escaped'),clock=lambda:at)

def test_refusal_incomplete_and_no_rag(store):
 p=packet(store);at=p['snapshot']['as_of']
 with patch.object(r,'prepare',return_value=p):out=r.review(store,p['snapshot']['id'],paid(store),transport=lambda _:dict(status='incomplete'),clock=lambda:at)
 assert out['status']=='REJECTED_RESPONSE'
 p=r.prepare(store,p['snapshot']['id'],variant='B_LLM_NO_RAG');assert not p['source_records'] and not p['audit_notes']
 assert all(not h.get('source_rule_ids') for h in p['hypotheses'].values())

def test_expiry_and_overbilling_halt(store):
 p=packet(store);at=p['snapshot']['as_of'];c=paid(store)
 with pytest.raises(ValueError,match='EXPIRED'):r.policy(c,at+2000)
 raw=response(answer(p));raw['usage']['input_tokens']=10000000
 with patch.object(r,'prepare',return_value=p):r.review(store,p['snapshot']['id'],c,transport=lambda _:raw,clock=lambda:at)
 with store.readonly() as d:assert d.execute("SELECT 1 FROM reasoning_control WHERE key='budget_halt'").fetchone()

def test_model_can_select_context_candidate_independent_of_detector_risk_still_blocks(store):
 p=packet(store);a=answer(p);chosen=next(k for k,v in p['hypotheses'].items() if v['symbol']=='NQ' and v['direction']=='LONG')
 evidence=next((i,e) for i,e in p['evidence'].items() if e.get('symbol')=='NQ' and 'close' in e);ident,e=evidence
 a.update(decision='READY_FOR_RISK_CHECK',preferred_hypothesis_id=chosen,selected_rule_ids=['fixture-rule'],missing_conditions=[],proposal=dict(symbol='NQ',contract=e['contract'],side='LONG',**{k:dict(evidence_id=ident,field=f,value=e[f]) for k,f in [('entry','close'),('stop','low'),('target','high')]}))
 assessment=next(x for x in a['assessments'] if x['hypothesis_id']==chosen);assessment.update(stance='SUPPORT',lifecycle='SUPPORTED',missing=[],supporting_market_ids=[ident],supporting_rule_ids=['fixture-rule'])
 v=r.validate(a,p,a['as_of']);assert v['valid'] and v['risk']['approved'] is False and v['orders_sent']==0
 a['proposal']['symbol']='MNQ'
 with pytest.raises(ValueError,match='IDENTITY'):r.validate(a,p,a['as_of'])

def test_paper_bridge_requires_explicit_policy_and_actual_recorded_candidate(store,tmp_path):
 from services.tanja.ict_agent.paper import PaperSession
 from dataclasses import asdict
 from services.tanja.ict_agent.tests.test_observer import policy,fixtures
 file=tmp_path/'policy.json';file.write_text(json.dumps(dict(mode='PAPER',approval_id='UNIT_TEST_ONLY',risk=asdict(policy()))))
 paper=PaperSession(store,tmp_path/'paper.sqlite3',file);p,a,m=fixtures();paper.reconcile(1000,{})
 with pytest.raises(ValueError):paper.consider(store.snapshot()['id'],'invented',a,m,1000)
 app=Flask(__name__);ict_ai_trader.register(app,dict(cfg(store),ICT_AI_PAPER_JOURNAL=str(tmp_path/'paper.sqlite3')),start=False)
 out=app.test_client().get('/ict-ai-trader/api/paper',headers=auth()).json;assert out['orders']==[] and out['actual_orders']==0

def test_existing_intake_worker_restart_no_paid_calls(tmp_path,monkeypatch):
 import sqlite3,time,hashlib
 from services.tanja.ict_agent.runtime import Worker
 from services.tanja.ict_agent import knowledge,runtime
 # Verify the real fingerprint before timing worker lifecycle. A cold/cloud-backed
 # source-tree read is not an observer startup/shutdown latency assertion.
 fingerprint=runtime.code_hash()
 monkeypatch.setattr(runtime,'code_hash',lambda:fingerprint)
 class K(FakeKnowledge):
  def __init__(self,*a,**k):pass
 monkeypatch.setattr(knowledge,'Knowledge',K)
 source=tmp_path/'intake.sqlite3'
 with sqlite3.connect(source) as d:
  for t in ('bars','collection_bars'):d.execute(f'CREATE TABLE {t}(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
  for minute in range(2):
   for sym in ('NQ','ES','MNQ'):
    b=bar(1777642200+minute*60,sym);t='collection_bars' if sym=='NQ' else 'bars'
    payload=dict(symbol=sym,confirmed=True,standard=True,bar_open_ms=b['ts']*1000,bar_close_ms=b['received_at']*1000,ticker=b['contract'],**{k:b[k] for k in ('open','high','low','close')})
    d.execute(f'INSERT INTO {t} VALUES(?,?,?,?,?)',(sym,b['ts'],b['received_at'],b['received_at'],json.dumps(payload)))
 original=hashlib.sha256(source.read_bytes()).hexdigest()
 config=dict(DATA_DIR=str(tmp_path),ICT_AI_SOURCE_DB=str(source),ICT_AI_MODE='OBSERVE')
 for attempt in range(2):
  w=Worker(config);w.start();deadline=time.monotonic()+10
  while time.monotonic()<deadline:
   target=tmp_path/'ict_ai/observe.sqlite3'
   if target.is_file():
    try:
     with sqlite3.connect(target) as d:n=d.execute('SELECT count(*) FROM bars').fetchone()[0]
     if n==6:break
    except sqlite3.Error:pass
   time.sleep(.03)
  w.stop();w.thread.join(5);assert not w.thread.is_alive()
  with sqlite3.connect(target) as d:assert d.execute('SELECT count(*) FROM bars').fetchone()[0]==6
 assert hashlib.sha256(source.read_bytes()).hexdigest()==original
 assert json.loads((tmp_path/'ict_ai/worker-status.json').read_text())['orders_enabled'] is False

def test_private_asset_hashes_and_escape(tmp_path):
 import hashlib
 from services.tanja.ict_agent.assets import verify
 p=tmp_path/'source.json';p.write_text('{}');m=tmp_path/'manifest.json';m.write_text(json.dumps({'files':{'source.json':hashlib.sha256(p.read_bytes()).hexdigest()}}))
 assert verify(tmp_path)
 p.write_text('changed')
 with pytest.raises(ValueError,match='HASH_MISMATCH'):verify(tmp_path)
 m.write_text(json.dumps({'files':{'../outside':'fake'}}))
 with pytest.raises(ValueError,match='HASH_MISMATCH'):verify(tmp_path)

def test_source_sequence_order_and_unknown_are_not_execution_permission():
 from services.tanja.ict_agent.sequences import match
 rid='full-assistant-kNlySn81dmo-003'
 assert match(rid,['STRUCTURE_BREAK','SWEEP'])['status']=='INCOMPLETE_CONCEPT_SEQUENCE'
 m=match(rid,['SWEEP','DISPLACEMENT','STRUCTURE_BREAK','FVG'])
 assert m['status']=='CONCEPT_ORDER_MATCH_ONLY' and m['executable'] is False
 assert match('unknown',[])['status']=='SOURCE_SEQUENCE_NOT_COMPILED'
 assert match('full-assistant-Cg0-CFJOJvg-004',['STRUCTURE_BREAK'])['missing']==[['RETRACEMENT']]

def test_weekly_levels_require_five_complete_sessions_and_do_not_peek():
 from datetime import datetime,timedelta
 from services.tanja.ict_agent.market import advance,fresh_state,NY,week_context,completed_week
 state=fresh_state('NQ','TEST');weekly=[];last=None
 # Week preceding the US DST switch; sessions stay anchored to New York time.
 monday=datetime(2026,3,2,17,tzinfo=NY)
 for day in range(5):
  end=monday+timedelta(days=day);start=end.replace(hour=18)-timedelta(days=1)
  for minute in range(1380):
   ts=int(start.timestamp())+minute*60
   frames,events=advance(state,dict(symbol='NQ',contract='TEST',ts=ts,received_at=ts+60,
       open=100,high=101+day,low=99-day,close=100,source='UNIT_TEST_SYNTHETIC'))
   weekly.extend(e for e in events if e['kind']=='PRIOR_WEEK_LEVEL')
   if day<4 or minute<1379:assert not weekly
  last=end
 assert len(weekly)==2 and {e['price'] for e in weekly}=={105,95}
 assert all(len(e['evidence_ids'])==5 and e['available_at']==int(last.timestamp()) for e in weekly)
 assert week_context(state,int(last.timestamp())-1)['status']=='UNAVAILABLE_INCOMPLETE_OR_NO_HISTORY'
 assert week_context(state,int(last.timestamp()))['status']=='LATEST_COMPLETE_WEEK'
 assert week_context(state,int((last+timedelta(days=7)).timestamp()))['status']=='LAST_AVAILABLE_WEEK_NOT_PREVIOUS'
 frames=state['frames']['1440']
 assert completed_week(frames[:2]+frames[3:]) is None
 changed=[dict(f) for f in frames];changed[2]['contract']='OTHER'
 assert completed_week(changed) is None
 # No holiday-calendar assumption can synthesize a missing complete session.
 assert completed_week(frames[:-1]) is None

def test_packet_requires_retained_week_and_session_anchors(store):
 from services.tanja.ict_agent.ai import packet as evidence_packet
 snapshot=store.snapshot()
 snapshot['context']['NQ']['previous_week']={'evidence_ids':['missing-older-week-frame']}
 with pytest.raises(ValueError,match='MISSING_MARKET_EVIDENCE'):evidence_packet(store,snapshot)
