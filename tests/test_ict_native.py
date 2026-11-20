import json
from html.parser import HTMLParser
from unittest.mock import patch
from flask import Flask
import pytest
import ict_shadow_cloud
from services.tanja.ict_agent import reasoning as r
from services.tanja.ict_agent.observer import Observer
from tests.test_ict_ai_trader import store,cfg,auth,answer,paid,response
from services.tanja.ict_agent.tests.test_observer import FakeKnowledge,bar


def test_native_shadow_component_and_bookmark_compatibility(store):
 app=Flask(__name__);ict_shadow_cloud.register(app,cfg(store),start=False);c=app.test_client()
 for query in ('','?view=live&review=saved-review','?view=history&period=week&date=2026-05-01&session=NYAM','?view=scanner&review=saved-review','?view=ictai'):
  assert c.get('/ict-shadow'+query).status_code==401
  page=c.get('/ict-shadow'+query,headers=auth());assert page.status_code==200
  assert page.headers['X-Frame-Options']=='DENY' and '<!--ICT_AI_TRADER_VIEW-->' not in page.text
  assert 'id="viewICTTrader"' in page.text and 'id="ictai-charts"' in page.text
 class Tags(HTMLParser):
  def __init__(self):super().__init__();self.ids=[];self.frames=0
  def handle_starttag(self,tag,attrs):
   self.frames+=tag=='iframe'
   for k,v in attrs:
    if k=='id':self.ids.append(v)
 tags=Tags();tags.feed(page.text)
 assert len(tags.ids)==len(set(tags.ids)) and tags.frames==0
 for route in ('/assets/app.js','/assets/native.css','/api/state','/api/budget','/api/session-report','/api/snapshots/'+store.snapshot()['id']+'/reasoning-history'):
  assert c.get('/ict-shadow/ai-trader'+route).status_code==401
  assert c.get('/ict-shadow/ai-trader'+route,headers=auth()).status_code==200
 assert c.get('/ict-shadow/ai-trader/api/state?mode=baseline',headers=auth()).json['mode']=='DETERMINISTIC_BASELINE'
 assert c.get('/ict-ai-trader',headers=auth()).headers['Location']=='/ict-shadow?view=ictai'
 assert c.post('/ict-shadow/ai-trader/api/state',headers=auth()).status_code==405


def test_reasoning_lifecycle_restart_asof_and_rejection(store):
 first=store.snapshot();p=r.prepare(store,first['id']);a=answer(p)
 out=r.review(store,first['id'],paid(store),transport=lambda _:response(a),clock=lambda:first['as_of'])
 assert out['status']=='VALIDATED_ADVISORY'
 Observer(store,FakeKnowledge()).push([bar(first['as_of']+300,symbol=x) for x in ('NQ','ES','MNQ')])
 second=store.snapshot();p=r.prepare(store,second['id']);assert p['prior_validated_reasoning']['snapshot_id']==first['id']
 a=answer(p);ident=next(i for i,e in p['evidence'].items() if e.get('symbol')=='NQ')
 a['changes']=dict(previous_snapshot_id=first['id'],market_ids=[ident],interpretation='Observed counterevidence',thesis='INVALIDATED')
 assessment=a['assessments'][0];assessment.update(lifecycle='INVALIDATED',stance='OPPOSE',opposing_market_ids=[ident])
 out=r.review(store,second['id'],paid(store),transport=lambda _:response(a),clock=lambda:second['as_of'])
 assert out['status']=='VALIDATED_ADVISORY'
 app=Flask(__name__);ict_shadow_cloud.register(app,cfg(store),start=False);c=app.test_client()
 old=c.get('/ict-shadow/ai-trader/api/snapshots/'+first['id']+'/reasoning-history',headers=auth()).json
 assert all(x['as_of']==first['as_of'] for x in old['transitions'])
 new=c.get('/ict-shadow/ai-trader/api/snapshots/'+second['id']+'/reasoning-history',headers=auth()).json
 assert any(x['from_state']=='WATCHING' and x['state']=='INVALIDATED' for x in new['transitions'])
 assert c.get('/ict-shadow/ai-trader/api/snapshots/'+second['id']+'/reasoning',headers=auth()).json['state']=='COMPLETED'
 with store.readonly() as db:
  count=db.execute('SELECT count(*) FROM reasoning_transitions').fetchone()[0]
  assert count==2*len(a['assessments'])
 # A fabricated invalidation cannot alter persistent state.
 assessment['opposing_market_ids']=[]
 with pytest.raises(ValueError,match='INVALIDATION_NEEDS'):r.validate(a,p,second['as_of'])


def test_frozen_experiment_packet_changes_block_before_transport(store):
 with pytest.raises(ValueError,match='FROZEN_EXPERIMENT_PACKET_CHANGED'):
  r.review(store,store.snapshot()['id'],paid(store),transport=lambda _:pytest.fail('unauthorized changed experiment'),clock=lambda:store.snapshot()['as_of'],expected_packet_hash='wrong')
 with store.readonly() as db:assert db.execute('SELECT count(*) FROM reasoning_budget').fetchone()[0]==0


def test_private_runtime_config_is_loaded_through_cloud_mount(store):
 settings=store.path.parent/'ict_ai/runtime-config.json'
 settings.parent.mkdir(exist_ok=True)
 settings.write_text(json.dumps({'ICT_AI_MODE':'OBSERVE','ICT_AI_API_KEY_ENV':'OPENAI_API_KEY'}))
 config=dict(cfg(store),OPENAI_API_KEY='fixture-not-a-real-key')
 with patch('services.tanja.ict_agent.runtime.Worker') as worker,patch.object(ict_shadow_cloud.Cloud,'start_worker'):
  ict_shadow_cloud.register(Flask(__name__),config,start=True)
  loaded=worker.call_args.args[0]
  assert loaded['ICT_AI_MODE']=='OBSERVE' and loaded['ICT_AI_API_KEY']=='fixture-not-a-real-key'
  worker.return_value.start.assert_called_once()
 settings.write_text(json.dumps({'ICT_AI_API_KEY':'forbidden'}))
 with pytest.raises(ValueError,match='INVALID_ICT_PRIVATE_CONFIG'):
  ict_shadow_cloud.register(Flask(__name__),config,start=False)


def test_external_session_window_uses_ny_bar_time_including_last_minute():
 from datetime import datetime
 from zoneinfo import ZoneInfo
 from services.tanja.ict_agent.session import in_session
 ny=ZoneInfo('America/New_York')
 for hour,minute,expected in [(9,29,False),(9,30,True),(11,59,True),(12,0,False)]:
  ts=datetime(2026,11,20,hour,minute,tzinfo=ny).timestamp()
  assert in_session(ts,'2026-11-20') is expected
  assert not in_session(ts,'2026-11-19')


def test_knowledge_cache_evicts_old_results_without_changing_answers():
 from collections import OrderedDict
 import numpy as np
 from services.tanja.ict_agent.knowledge import Knowledge
 class Index:
  def scores(self,text):return np.zeros(0),np.zeros(0)
 k=Knowledge.__new__(Knowledge);k.cache=OrderedDict();k.index=Index();k.rules=[];k.verified=set()
 k.notes=[];k.families=[];k.version='fixture';k.index_version='fixture';k.retrieval_kind='fixture';k.case_ids=set()
 first=k.retrieve('query0')
 for i in range(1,300):k.retrieve('query'+str(i))
 assert len(k.cache)==256 and ('query0','ANY',(),5) not in k.cache
 assert k.retrieve('query0')==first and len(k.cache)==256


def test_external_replay_pages_past_missing_feed_and_excludes_future(tmp_path,monkeypatch):
 import sqlite3
 from services.tanja.ict_agent import session
 class K(FakeKnowledge):
  audit={}
  def __init__(self,**kwargs):pass
 monkeypatch.setattr(session,'Knowledge',K);monkeypatch.setattr(session,'verify',lambda _:None)
 monkeypatch.setattr(session,'code_hash',lambda:'fixture')
 source=tmp_path/'source.sqlite3';base=1777642200
 with sqlite3.connect(source) as db:
  for table in ('bars','collection_bars'):
   db.execute(f'CREATE TABLE {table}(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
  for i in range(516):
   for symbol in (('ES',) if i<512 else ('ES','NQ','MNQ')):
    b=bar(base+i*60,symbol);payload=dict(symbol=symbol,ticker=b['contract'],confirmed=True,standard=True,
      bar_open_ms=b['ts']*1000,bar_close_ms=(b['ts']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
    db.execute('INSERT INTO '+('collection_bars' if symbol=='NQ' else 'bars')+' VALUES(?,?,?,?,?)',
       (symbol,b['ts'],b['ts']+60,b['received_at'],json.dumps(payload)))
 s,k,report=session.build(source,tmp_path/'run/observer.sqlite3',tmp_path,'2026-05-01',base+515*60)
 assert report['processed']==3 and len(report['gaps'])==512
 with s.readonly() as db:
  assert db.execute('SELECT count(*) FROM bars').fetchone()[0]==9
  assert db.execute('SELECT max(available_at) FROM bars').fetchone()[0]<=base+515*60
