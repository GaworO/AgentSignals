import copy
import pytest
from ict_shadow_research import validate,window,schema,DAY
from datetime import datetime
from zoneinfo import ZoneInfo
T=datetime.fromisoformat(DAY+'T09:35:00').replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
def sample(short=False):
 e={}
 for i,(o,h,l,c) in enumerate([(102,104,100,103),(103,108,102,107),(107,110,106,109)]):
  e[str(i)]=dict(kind='bar',symbol='MNQ',timeframe=1,time=T-180+i*60,available_at=T-120+i*60,open=o,high=h,low=l,close=c)
 e['target']=dict(kind='level',symbol='MNQ',price=116,available_at=T-100)
 if short:
  for b in e.values():
   if b['kind']=='bar':b.update(open=220-b['open'],high=220-b['low'],low=220-b['high'],close=220-b['close'])
   else:b['price']=220-b['price']
 packet=dict(packet_id='test',as_of=T,evidence=e)
 p=dict(direction='SHORT' if short else 'LONG',order_type='LIMIT',entry_fvg_bar_ids=['0','1','2'],entry_fraction=.5,entry_price=115 if short else 105,stop_anchor=dict(evidence_id='0',field='high' if short else 'low',price=120 if short else 100),stop_buffer_ticks=1,stop_price=120.25 if short else 99.75,target_anchor=dict(evidence_id='target',field='price',price=104 if short else 116),target_price=104 if short else 116,valid_until=T+600)
 d=dict(packet_id='test',execution_authorized=False,decision='PROPOSE_'+p['direction'],assessments={'context':dict(evidence_ids=['0'])},plan=p)
 return packet,d
@pytest.mark.parametrize('short',[False,True])
def test_valid(short):
 p,d=sample(short);v=validate(p,d,T+10);assert v['state']=='VALIDATED_SHADOW_PROPOSAL' and not v['execution_authorized'];assert v['risk_usd_per_mnq']==10.5
@pytest.mark.parametrize('decision',['WAIT','NO_TRADE'])
def test_wait(decision):
 p,d=sample();d.update(decision=decision,plan=None);assert validate(p,d,T+10)['state']==decision
@pytest.mark.parametrize('mutation', ['id','permission','expired','tick','entry','symbol','future','invention','rr','stop','target','gap','response_stale'])
def test_reject(mutation):
 p,d=sample();at=T+10
 if mutation=='id':d['packet_id']='wrong'
 if mutation=='permission':d['execution_authorized']=True
 if mutation=='expired':d['plan']['valid_until']=T+1
 if mutation=='tick':d['plan']['entry_price']=105.1
 if mutation=='entry':d['plan']['entry_price']=105.25
 if mutation=='symbol':p['evidence']['0']['symbol']='NQ'
 if mutation=='future':p['evidence']['2']['available_at']=T+60
 if mutation=='invention':d['assessments']['context']['evidence_ids']=['missing']
 if mutation=='rr':p['evidence']['target']['price']=110;d['plan']['target_price']=110;d['plan']['target_anchor']['price']=110
 if mutation=='stop':d['plan']['stop_price']=100
 if mutation=='target':d['plan']['target_price']=117
 if mutation=='gap':p['evidence']['1']['time']-=60
 if mutation=='response_stale':at=T+300
 with pytest.raises((AssertionError,KeyError)):validate(p,d,at)
def test_post_fvg_stop_breach():
 p,d=sample();p['as_of']=T+60;p['evidence']['later']=dict(kind='bar',symbol='MNQ',timeframe=1,time=T,available_at=T+60,open=105,high=107,low=99,close=106)
 with pytest.raises(AssertionError,match='BREACHED'):validate(p,d,T+70)
def test_budget_slots_preserved():
 import sys
 sys.path.insert(0,'/private/tmp/ai-packet-fix-20261030')
 from directional_hybrid_context import budget_reason
 rows=[{'review_kind':'CONTEXT'}]
 assert budget_reason(rows,0,'POSITION') is None
 rows.extend([{'review_kind':'POSITION'}]*2)
 assert budget_reason(rows,0,'POSITION')=='POSITION_ALLOCATION_EXHAUSTED'
 assert budget_reason(rows,0,'SETUP') is None
 for i in range(7):rows.append({'review_kind':'SETUP'})
 assert budget_reason(rows,0,'SETUP')=='SETUP_ALLOCATION_EXHAUSTED'
 assert budget_reason(rows,0,'CONTEXT') is None
 rows.extend([{'review_kind':'CONTEXT'}]*2)
 assert budget_reason(rows,0,'POSITION')=='DAILY_LIMIT_REACHED'
def test_window():
 window(0,T);window(6,T+60*60);window(7,T+80*60)
 for index,at in [(0,T-6*60),(0,T+80*60),(7,T),(7,T+90*60),(0,T+86400)]:
  with pytest.raises(AssertionError):window(index,at)
def test_bound_schema():assert schema('abc')['properties']['packet_id']['enum']==['abc']

def manual_auth(at):
 return dict(day_ny=DAY,scope='ONE_ADDITIONAL_SHADOW_REVIEW',execution_authorized=False,authorized_at=at-10,request_before=at+300,maximum_paid_calls=8,slot=5)

def test_explicit_one_off_does_not_extend_scheduler():
 from ict_shadow_research import manual_slot,choose_slot
 at=T+100*60
 assert choose_slot([],{},at)[1]=='OUTSIDE_SESSION'
 assert manual_slot([],manual_auth(at),at)[:2]==(5,'USER_REQUESTED_FOLLOWUP')
 with pytest.raises(AssertionError):window(5,at)
 with pytest.raises(AssertionError):manual_slot([],manual_auth(at),at+301)
 rows=[dict(id='x_5',requested_at=at,status='research_completed')]
 assert manual_slot(rows,manual_auth(at),at)[1]=='MANUAL_ALREADY_ATTEMPTED'
 rows=[dict(id='x_'+str(i),requested_at=at-1200,status='research_completed') for i in range(8) if i!=5]+[dict(id='x_8',requested_at=at-1200,status='research_completed')]
 assert manual_slot(rows,manual_auth(at),at)[1]=='EIGHT_CALL_CAP'
 assert manual_slot([dict(id='x_1',requested_at=at-20,status='research_inflight')],manual_auth(at),at)[1]=='RESEARCH_INFLIGHT_RECONCILE'

def test_manual_proposal_freshness_and_expiry_still_enforced():
 p,d=sample();at=T+100*60
 for b in p['evidence'].values():
  b['available_at']+=100*60
  if 'time' in b:b['time']+=100*60
 p.update(as_of=at,source_observed_at=at,assessment_kind='USER_REQUESTED_FOLLOWUP',manual_authorization=manual_auth(at));d['plan']['valid_until']=at+600
 assert validate(p,d,at+10)['state']=='VALIDATED_SHADOW_PROPOSAL'
 with pytest.raises(AssertionError):validate(p,d,at+300)
 d['plan']['valid_until']=at+1801
 with pytest.raises(AssertionError):validate(p,d,at+10)
 d['plan']['valid_until']=at+600;p.pop('assessment_kind')
 with pytest.raises(AssertionError):validate(p,d,at+10)

def pm_auth():return dict(day_ny=DAY,scope='NYPM_TWO_REMAINING_RESEARCH_CALLS',maximum_paid_calls=8,execution_authorized=False)
def nytime(h,m):return datetime.fromisoformat(DAY+f'T{h:02}:{m:02}:00').replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
def research_rows(n):return [dict(id=f'x_{i}',requested_at=T,status='research_completed') for i in range(n)]
def test_pm_opening_and_remaining_two_budget():
 from ict_shadow_research import pm_slot
 a=pm_auth();r=research_rows(6);p=dict(as_of=nytime(13,35),trigger_events=[])
 assert pm_slot(r,p,a,nytime(13,29))[1]=='OUTSIDE_NYPM'
 assert pm_slot(r,dict(as_of=nytime(13,30)),a,nytime(13,30))[1]=='WAIT_FIRST_CLOSED_NYPM_BAR'
 assert pm_slot(r,p,a,nytime(13,35))[:2]==(6,'NYPM_OPENING')
 r.append(dict(id='x_6',requested_at=nytime(13,35),status='research_completed'))
 assert pm_slot(r,p,a,nytime(13,40))[1]=='TEN_MINUTE_COOLDOWN'
 assert pm_slot(r,p,a,nytime(14,20))[1]=='WAIT_NYPM_SECOND_HALF'
 p.update(as_of=nytime(14,35),trigger_events=[dict(available_at=nytime(14,30))])
 assert pm_slot(r,p,a,nytime(14,35))[:2]==(7,'NYPM_STRUCTURE_EVENT')
 p['trigger_events']=[]
 assert pm_slot(r,p,a,nytime(14,35))[1]=='WAIT_NYPM_EVENT_OR_FOLLOWUP'
 assert pm_slot(r,p,a,nytime(15,30))[:2]==(7,'NYPM_FOLLOWUP')
 r.append(dict(id='x_7',requested_at=nytime(15,30),status='research_unknown'))
 assert pm_slot(r,p,a,nytime(15,45))[1]=='EIGHT_CALL_CAP'
 assert pm_slot(r,p,a,nytime(16,0))[1]=='OUTSIDE_NYPM'
 assert pm_slot(r,p,a,nytime(13,35)+86400)[1]=='OUTSIDE_NYPM'
def test_pm_inflight_duplicate_and_auth():
 from ict_shadow_research import pm_slot
 r=research_rows(6);r[0]['status']='research_inflight';p={'as_of':nytime(13,35)}
 assert pm_slot(r,p,pm_auth(),nytime(13,35))[1]=='RESEARCH_INFLIGHT_RECONCILE'
 a=pm_auth();a['maximum_paid_calls']=9
 with pytest.raises(AssertionError):pm_slot([],p,a,nytime(13,35))
def test_pm_geometry_validity_ends_at_session_end():
 p,d=sample();at=nytime(15,55);delta=at-T
 for b in p['evidence'].values():
  b['available_at']+=delta
  if 'time' in b:b['time']+=delta
 p.update(as_of=at,source_observed_at=at,assessment_kind='NYPM_FOLLOWUP',pm_authorization=pm_auth());d['plan']['valid_until']=nytime(16,0)
 assert validate(p,d,at+10)['state']=='VALIDATED_SHADOW_PROPOSAL'
 d['plan']['valid_until']+=60
 with pytest.raises(AssertionError):validate(p,d,at+10)

def shared_auth():
 from ict_shadow_research import SHARED_SCOPE
 return dict(pm_auth(),scope=SHARED_SCOPE,max_borrowed_calls=3,reserve_setup_recovery=4,reserve_position=2)
def test_shared_pm_selection_preserves_two_dedicated_slots():
 from ict_shadow_research import pm_slot
 a=shared_auth();r=research_rows(7);r[-1]['requested_at']=nytime(13,35)
 p=dict(as_of=nytime(13,55),trigger_events=[])
 assert pm_slot(r,p,a,nytime(13,55))[:2]==(8,'NYPM_BORROWED_REVIEW')
 r.append(dict(id='x_8',requested_at=nytime(13,55),status='research_unknown'))
 p.update(as_of=nytime(14,35),trigger_events=[dict(available_at=nytime(14,34))])
 assert pm_slot(r,p,a,nytime(14,35))[:2]==(7,'NYPM_STRUCTURE_EVENT')
 r.append(dict(id='x_7',requested_at=nytime(14,35),status='research_completed'));p['as_of']=nytime(14,55);p['trigger_events']=[]
 assert pm_slot(r,p,a,nytime(14,55))[:2]==(9,'NYPM_BORROWED_REVIEW')
 r.extend([dict(id='x_9',requested_at=nytime(14,55),status='research_completed'),dict(id='x_10',requested_at=nytime(15,15),status='research_completed')])
 assert pm_slot(r,p,a,nytime(15,45))[1]=='NYPM_REVIEWS_COMPLETE'

def production_db(tmp_path):
 import sqlite3
 path=tmp_path/'production.sqlite'
 with sqlite3.connect(path) as db:
  db.execute('CREATE TABLE reviews(id TEXT PRIMARY KEY,day TEXT,started REAL,cutoff REAL,status TEXT,request TEXT,decision TEXT,usage TEXT,invalidated TEXT,review_kind TEXT,prompt_version TEXT,model TEXT,requested_at REAL,finished REAL,error_code TEXT,response_model TEXT)')
  db.execute('CREATE TABLE setup_requests(order_id TEXT PRIMARY KEY,state TEXT,expires REAL,review_id TEXT)')
  for i in range(2):db.execute('INSERT INTO reviews(id,day,status,review_kind) VALUES(?,?,?,?)',(f'context{i}',DAY,'rejected','CONTEXT'))
 return path

def test_borrowed_budget_atomic_debits_preserve_real_production_capacity(tmp_path):
 import sqlite3,sys,json
 from ict_shadow_research import reserve_borrowed_call,PREFIX,finish_borrowed_call,BUDGET_PREFIX
 sys.path.insert(0,'/private/tmp/ai-packet-fix-20261030')
 from directional_hybrid_context import budget_reason
 path=production_db(tmp_path);at=nytime(14,0)
 for i in (8,9,10):
  debit=reserve_borrowed_call(path,0,at,i,PREFIX+str(i),at-60,'mock')
  assert debit['allowed']
  assert not reserve_borrowed_call(path,0,at,i,PREFIX+str(i),at-60,'mock')['allowed']
 finish_borrowed_call(path,BUDGET_PREFIX+'8',at+10,{'usage':{'total_tokens':5},'model':'mock'},None)
 with sqlite3.connect(path) as db:
  db.row_factory=sqlite3.Row;rows=[dict(r) for r in db.execute('SELECT * FROM reviews')]
  assert db.execute('SELECT count(*) FROM setup_requests').fetchone()[0]==0
  debits=[r for r in rows if r['id'].startswith(BUDGET_PREFIX)]
  assert len(debits)==3 and all(r['decision'] is None and r['status'].startswith('research_budget_') and json.loads(r['request'])['execution_authorized'] is False for r in debits)
 # Unmodified production allocation must still accept NYPM context,
 # FOUR actual setup/recovery calls and TWO actual emergency calls.
 for kind in ['CONTEXT']+['SETUP']*4+['POSITION']*2:
  assert budget_reason(rows,0,kind) is None
  rows.append(dict(review_kind=kind))
 assert len(rows)==12 and budget_reason(rows,0,'SETUP')=='DAILY_LIMIT_REACHED'

@pytest.mark.parametrize('mode',['external','pending','running','position_used','setup_used','bad_cap'])
def test_borrow_blocks_without_capacity(tmp_path,mode):
 import sqlite3
 from ict_shadow_research import reserve_borrowed_call,PREFIX,spare_budget
 path=production_db(tmp_path);at=nytime(14,0);external=3 if mode=='external' else 0
 with sqlite3.connect(path) as db:
  if mode=='pending':db.execute('INSERT INTO setup_requests VALUES(?,?,?,?)',('order','queued',at+60,None))
  if mode=='running':db.execute("UPDATE reviews SET status='running' WHERE id='context0'")
  if mode in ('position_used','setup_used'):
   for i in range(1 if mode=='position_used' else 3):db.execute('INSERT INTO reviews(id,day,status,review_kind) VALUES(?,?,?,?)',(f'actual{i}',DAY,'validated','POSITION' if mode=='position_used' else 'SETUP'))
 if mode=='bad_cap':assert spare_budget([],0,13)['available']==0;return
 assert not reserve_borrowed_call(path,external,at,8,PREFIX+'8',at-60,'mock')['allowed']
 with sqlite3.connect(path) as db:assert db.execute("SELECT count(*) FROM reviews WHERE id LIKE 'ICT_RESEARCH_BUDGET_%'").fetchone()[0]==0

def test_borrow_concurrent_same_request_and_restart(tmp_path):
 from concurrent.futures import ThreadPoolExecutor
 import sqlite3
 from ict_shadow_research import reserve_borrowed_call,PREFIX
 path=production_db(tmp_path);at=nytime(14,0)
 def reserve():return reserve_borrowed_call(path,0,at,8,PREFIX+'8',at-60,'mock')
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:reserve(),range(2)))
 assert sum(r['allowed'] for r in results)==1
 # Even if no research result was saved after this debit, it cannot be resent.
 assert reserve()['reason']=='BUDGET_ALREADY_RESERVED'
 with sqlite3.connect(path) as db:assert db.execute('SELECT count(*) FROM reviews').fetchone()[0]==3

def test_borrow_rejects_unknown_usage_and_wrong_session(tmp_path):
 from ict_shadow_research import reserve_borrowed_call,PREFIX,spare_budget
 assert spare_budget([],None)['available']==0
 assert spare_budget([],True)['available']==0
 path=production_db(tmp_path)
 with pytest.raises(AssertionError):reserve_borrowed_call(path,0,nytime(12,0),8,PREFIX+'8',T,'mock')

def test_real_packet_preparation_no_paid_call():
 from pathlib import Path
 import json
 from ict_shadow_research import PROMPT,canonical,digest
 packet=json.loads((Path(__file__).parent/'fixtures/ict_shadow_market.json').read_text())['packet']
 packet['packet_id']=digest(packet)
 payload=dict(input=[dict(role='system',content=PROMPT),dict(role='user',content=canonical(packet))],text={'format':{'schema':schema(packet['packet_id'])}})
 assert len(canonical(payload).encode())<520000
 assert all(e['available_at']<=packet['as_of'] for e in packet['evidence'].values())


def row(index,at,status='research_completed',decision='{}'):
 return dict(id='ICT_INDEPENDENT_NYAM_20261030_'+str(index),requested_at=at,status=status,decision=decision)

def test_opening_once_and_cooldown():
 from ict_shadow_research import choose_slot
 p=dict(as_of=T,trigger_events=[])
 assert choose_slot([],p,T)[0]==0
 assert choose_slot([row(0,T)],p,T+60)[1]=='TEN_MINUTE_COOLDOWN'
 assert choose_slot([row(0,T)],dict(as_of=T+900,trigger_events=[]),T+900)[0]==5

def test_event_dedup_and_window_slots():
 from ict_shadow_research import choose_slot
 p=dict(as_of=T+600,trigger_events=[dict(available_at=T+590)])
 assert choose_slot([row(0,T)],p,T+600)[0]==1
 assert choose_slot([row(0,T),row(1,T+600)],p,T+1200)[0] is None
 assert choose_slot([row(0,T)],dict(as_of=T+1500,trigger_events=[dict(available_at=T+1490)]),T+1500)[0]==2

def test_eight_hard_cap_and_unknown_inflight():
 from ict_shadow_research import choose_slot
 p=dict(as_of=T+4800,trigger_events=[])
 assert choose_slot([row(i,T+i*60) for i in range(8)],p,T+4800)[1]=='EIGHT_CALL_CAP'
 assert choose_slot([row(0,T,'research_inflight')],p,T+1000)[1]=='RESEARCH_INFLIGHT_RECONCILE'
 assert choose_slot([row(0,T,'research_unknown')],p,T+4800)[0]==7

def test_close_once_and_outside_day():
 from ict_shadow_research import choose_slot
 p=dict(as_of=T+4800,trigger_events=[])
 assert choose_slot([],p,T+4800)[0]==7
 assert choose_slot([row(7,T+4800)],p,T+4900)[0] is None
 assert choose_slot([],p,T+86400)[1]=='OUTSIDE_SESSION'

def test_research_ledger_separate_from_production():
 from pathlib import Path
 source=(Path(__file__).parent.parent/'ict_shadow_research.py').read_text()
 assert "dbpath=root/'research_budget.sqlite3'" in source
 assert "?mode=ro',uri=True) as prod" in source
 assert "'EIGHT_CALL_CAP_INCLUDING_LEGACY'" in source
