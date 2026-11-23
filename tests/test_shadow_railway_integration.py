import base64,json,os,sqlite3,time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from copy import deepcopy
import pytest
from flask import Flask,request
from services.tanja.ict_agent.shadow_gateway import register,export_candles
from services.tanja.ict_agent.shadow_transport import PrivateReader,ReadFailure
from services.ict_shadow.worker import Poller,check_environment,create_app,safe_status
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.prospective_shadow import PassiveObserver

TOKEN='test-only-not-a-production-secret-123456'
AUTH={'Authorization':'Basic '+base64.b64encode(b'tanja:offline-fixture').decode()}
class K:
    version=index_version='OFFLINE_TEST_ONLY';verified=set()
    def retrieve(self,*a,**kw):return {'records':[],'audit_notes':[]}

@pytest.fixture
def intake(tmp_path):
    p=tmp_path/'market_context/tanja.sqlite3';p.parent.mkdir()
    with sqlite3.connect(p) as d:
        d.execute('CREATE TABLE metadata(key,value)');d.execute('INSERT INTO metadata VALUES(?,?)',('tickers',json.dumps({s:s+'1!' for s in ('NQ','MNQ','ES')})))
        for t in ('bars','collection_bars'):d.execute('CREATE TABLE '+t+'(symbol,start,end,received,payload)')
        for s in ('NQ','MNQ','ES'):
            for ts in (60,120,180):
                v=dict(symbol=s,ticker=s+'1!',confirmed=True,standard=True,bar_open_ms=ts*1000,bar_close_ms=(ts+60)*1000,open=101,high=103,low=100,close=102,secret='MUST_NOT_LEAK')
                d.execute('INSERT INTO '+('collection_bars' if s=='NQ' else 'bars')+' VALUES(?,?,?,?,?)',(s,ts,ts+60,ts+61,json.dumps(v)))
    return p


def test_existing_auth_no_mutations_no_order_proxy_and_bounded_export(intake,tmp_path):
    app=Flask(__name__);seen=[]
    class R:
        def get(self,path):seen.append(path);return b'{"orders":false}', 'application/json'
    cfg=dict(DATA_DIR=str(tmp_path),ICT_SHADOW_UI_ENABLED='true',ICT_SHADOW_EXPORT_ENABLED='true',ICT_SHADOW_FEED_TOKEN=TOKEN)
    register(app,cfg,lambda:request.headers.get('Authorization')==AUTH['Authorization'],reader=R())
    c=app.test_client()
    assert c.get('/ict-shadow/shadow-mvp/status').status_code==401
    assert c.get('/internal/ict-shadow/candles?after=0',headers=AUTH).status_code==401
    assert c.get('/ict-shadow/shadow-mvp/status',headers={'Authorization':'Bearer '+TOKEN}).status_code==401
    assert c.get('/ict-shadow/shadow-mvp/status',headers=AUTH).status_code==200
    for route in ('/ict-shadow/shadow-mvp/orders','/ict-shadow/shadow-mvp/../../orders','/ict-shadow/shadow-mvp/status?url=http://broker'):
        assert c.get(route,headers=AUTH).status_code==400
    assert c.post('/ict-shadow/shadow-mvp/status',headers=AUTH).status_code==405
    before=intake.read_bytes();v=export_candles(intake,0,181)
    assert len(v['rows'])==6 and max(r[2] for r in v['rows'])==180
    assert 'MUST_NOT_LEAK' not in json.dumps(v) and intake.read_bytes()==before
    assert seen==['/ict-shadow/shadow-mvp/status']

@pytest.mark.parametrize('origin',['https://api.openai.com','http://tradovate.com','http://127.0.0.1','http://user:pw@x.railway.internal','http://x.railway.internal/path'])
def test_private_origin_rejects_provider_broker_and_credentials(origin):
    with pytest.raises(ValueError):PrivateReader(origin,TOKEN)


def test_credentials_and_execution_settings_fail_closed():
    for k in ('OPENAI_API_KEY','TRADOVATE_PASSWORD','AUTO_SUBMIT','BROKER_TOKEN','RAILWAY_TOKEN'):
        with pytest.raises(ValueError):check_environment({k:'must-not-log'})
    check_environment({'ICT_SHADOW_FEED_TOKEN':TOKEN,'ICT_SHADOW_VIEW_TOKEN':TOKEN+'-separate','PORT':'8090'})
    with pytest.raises(ValueError,match='SEPARATE_SERVICE_SCOPES_REQUIRED'):
        check_environment({'ICT_SHADOW_FEED_TOKEN':TOKEN,'ICT_SHADOW_VIEW_TOKEN':TOKEN})


def test_native_views_preserve_legacy_and_knowledge_without_duplicate_dashboard(tmp_path):
    import ict_ai_trader
    from services.tanja.ict_agent.shadow_panel import render
    app=Flask(__name__)
    for mode,marker in [('prospective','shadow-mvp-panel'),('episode6','episode6-panel'),('research','strategy-research-panel')]:
        with app.test_request_context('/ict-shadow?view=ictai&shadow_run='+mode):
            html=ict_ai_trader.render_shadow(Path('ict_shadow.html').read_text())
            assert marker in html and 'viewICTKnowledge' in html and 'viewICTTrader' in html
            assert html.count('id="'+marker+'"')==1
            assert 'ictai-mode' not in html
    with app.test_request_context('/ict-shadow?view=ictai&ai_snapshot=abc'):
        html=ict_ai_trader.render_shadow(Path('ict_shadow.html').read_text())
        assert 'ictai-mode' in html and 'ictknowledge' in html


def archived_batches():
    source=Path(os.environ['ICT_TEST_REPLAY_DB'])
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db:
        lo=int(datetime(2026,5,20,2,tzinfo=ZoneInfo('America/New_York')).timestamp());hi=int(datetime(2026,5,20,16,tzinfo=ZoneInfo('America/New_York')).timestamp())
        rows=db.execute('SELECT payload FROM bars WHERE ts>=? AND ts<? ORDER BY ts,symbol',(lo,hi)).fetchall()
    g={}
    for raw, in rows:
        b=json.loads(raw);g.setdefault(b['ts'],[]).append(b)
    return [v for v in g.values() if len(v)==3]


def envelope_for(batches,now):
    rows=[]
    for batch in batches:
        for b in batch:
            p={k:b[k] for k in ('symbol','open','high','low','close')};p.update(ticker=b['symbol']+'1!',bar_open_ms=b['ts']*1000,bar_close_ms=(b['ts']+60)*1000,confirmed=True,standard=True)
            rows.append([b['symbol'],b['ts'],b['ts']+60,b['ts']+61,json.dumps(p,sort_keys=True,separators=(',',':'))])
    return dict(read_only=True,bindings={s:s+'1!' for s in ('NQ','MNQ','ES')},captured_at=now,rows=rows,page_end=max(r[1] for r in rows),feed_health={s:None for s in ('NQ','MNQ','ES')})


def test_real_session_private_intake_restart_dedupe_and_actual_event_chain(tmp_path):
    batches=archived_batches();assert len(batches)==840
    root=tmp_path/'run';root.mkdir();clock=[batches[0][0]['ts']+62]
    report={'instrument_binding':{'tickers':{s:s+'1!' for s in ('NQ','MNQ','ES')}},'exchange_calendar':{'open_intervals_utc':[[0,2**62]]},'classification':'FEED_UNVERIFIED'}
    st=Store(root/'observer.sqlite3',dict(mode='OFFLINE_ADAPTER_TEST',orders_enabled=False,paid_calls_enabled=False))
    obs=PassiveObserver(st,K(),report,now_ns=lambda:int(clock[0]*1e9))
    class R:
        calls=[]
        def get(self,path):
            self.calls.append(path)
            return json.dumps(env).encode(),'application/json'
    reader=R();poll=Poller(obs,root/'mirror.sqlite3',root,reader,batches[0][0]['ts'],2**62,clock=lambda:clock[0],clock_ns=lambda:int(clock[0]*1e9))
    for i,batch in enumerate(batches):
        clock[0]=batch[0]['ts']+62;env=envelope_for([batch],clock[0])
        poll.cycle()
        if i==400:
            cursor=poll.cursor;obs.close();obs=PassiveObserver(Store(st.path),K(),report,now_ns=lambda:int(clock[0]*1e9));poll=Poller(obs,root/'mirror.sqlite3',root,reader,batches[0][0]['ts'],2**62,clock=lambda:clock[0],clock_ns=lambda:int(clock[0]*1e9));assert poll.cursor==cursor
    poll.cycle()
    with st.readonly() as db:
        assert db.execute("SELECT count(*) FROM passive_log WHERE kind='DURABLE'").fetchone()[0]==840
        assert db.execute('SELECT count(*) FROM bars').fetchone()[0]==2520
        assert db.execute("SELECT count(*) FROM shadow_records WHERE stage='DETECTED'").fetchone()[0]>0
        assert db.execute("SELECT count(*) FROM shadow_records WHERE stage='OUTCOME'").fetchone()[0]>0
        assert db.execute("SELECT count(*) FROM shadow_records WHERE json_extract(payload,'$.source_qualified')=1").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM passive_log WHERE kind='DURABLE' AND json_extract(payload,'$.prospective_timing_verified')=1").fetchone()[0]==0
    obs.close()


def test_worker_view_rejects_service_feed_token_post_and_secret_paths(tmp_path,monkeypatch):
    monkeypatch.setenv('ICT_SHADOW_VIEW_TOKEN',TOKEN)
    cfg=dict(root=str(tmp_path/'run'),runtime=str(tmp_path/'runtime'),replay_db=os.environ['ICT_TEST_REPLAY_DB'])
    Path(cfg['runtime']).mkdir();p=tmp_path/'cfg.json';p.write_text(json.dumps(cfg))
    app=create_app(p,start=False);c=app.test_client()
    assert c.get('/ict-shadow/shadow-mvp/status').status_code==401
    assert c.get('/ict-shadow/shadow-mvp/status',headers={'Authorization':'Bearer wrong'}).status_code==401
    h={'Authorization':'Bearer '+TOKEN}
    assert c.post('/ict-shadow/shadow-mvp/status',headers=h).status_code==405
    assert c.get('/orders',headers=h).status_code==404
    v=c.get('/ict-shadow/shadow-mvp/candidates?offset=0',headers=h)
    assert v.status_code==200 and len(v.json['items'])<=50
    assert '/private/' not in v.get_data(as_text=True)


def test_real_saved_long_short_evidence_delivered_by_private_api(tmp_path,monkeypatch):
    monkeypatch.setenv('ICT_SHADOW_VIEW_TOKEN',TOKEN)
    cfg=dict(root=str(tmp_path/'run'),runtime=str(tmp_path/'runtime'),replay_db=os.environ['ICT_TEST_REPLAY_DB']);Path(cfg['runtime']).mkdir()
    path=tmp_path/'cfg.json';path.write_text(json.dumps(cfg));app=create_app(path,start=False);c=app.test_client();h={'Authorization':'Bearer '+TOKEN}
    with sqlite3.connect(Path(cfg['replay_db']).as_uri()+'?mode=ro',uri=True) as db:
        selected=[db.execute("SELECT candidate_id FROM shadow_records WHERE stage='ASSESSMENT' AND json_extract(payload,'$.direction')=? ORDER BY as_of LIMIT 1",(side,)).fetchone()[0] for side in ('LONG','SHORT')]
    for ident in selected:
        r=c.get('/ict-shadow/shadow-mvp/candidates/'+ident,headers=h);assert r.status_code==200
        d=r.json;assert d['chart']['frames']['1'] and d['rules']
        assert d['latest']['semantic_status']=='PENDING_LLM' and d['latest']['execution_eligible'] is False
        assert all(v['available_at']<=d['chart']['as_of'] for v in d['chart']['annotations'])
        assert '/private/' not in r.get_data(as_text=True)


def test_read_failure_keeps_cursor_and_durable_prefix(tmp_path):
    b=archived_batches()[0];root=tmp_path/'run';root.mkdir();now=b[0]['ts']+62
    report={'instrument_binding':{'tickers':{s:s+'1!' for s in ('NQ','MNQ','ES')}},'exchange_calendar':{'open_intervals_utc':[[0,2**62]]},'classification':'FEED_UNVERIFIED'}
    st=Store(root/'observer.sqlite3',dict(mode='OFFLINE_ADAPTER_TEST',orders_enabled=False,paid_calls_enabled=False));obs=PassiveObserver(st,K(),report,now_ns=lambda:int(now*1e9))
    class R:
        fail=True
        def get(self,p):
            if self.fail:raise ReadFailure('READ_TIMEOUT_OR_DISCONNECT')
            return json.dumps(envelope_for([b],now)).encode(),'application/json'
    r=R();poll=Poller(obs,root/'mirror.sqlite3',root,r,b[0]['ts'],2**62,clock=lambda:now,clock_ns=lambda:int(now*1e9));before=poll.cursor
    with pytest.raises(ReadFailure):poll.cycle()
    assert poll.cursor==before
    with st.readonly() as db:assert db.execute('SELECT count(*) FROM transport_audit').fetchone()[0]==0
    r.fail=False;poll.cycle();poll.cycle()
    with st.readonly() as db:
        assert db.execute("SELECT count(*) FROM passive_log WHERE kind='DURABLE'").fetchone()[0]==1
    with st.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM transport_audit')
    obs.close()


def test_http_transport_never_redirects_retries_or_discloses_credentials(monkeypatch):
    import services.tanja.ict_agent.shadow_transport as t
    calls=[]
    class Reply:
        status=302
        def getheader(self,*_):return 'http://broker.invalid/order'
    class C:
        def __init__(self,*a,**kw):pass
        def request(self,method,path,headers):calls.append((method,path))
        def getresponse(self):return Reply()
        def close(self):pass
    monkeypatch.setattr(t.http.client,'HTTPConnection',C)
    with pytest.raises(ReadFailure,match='UPSTREAM_HTTP_302') as e:PrivateReader('http://agent-50k.railway.internal:8080',TOKEN).get('/internal/ict-shadow/candles?after=60')
    assert TOKEN not in str(e.value) and calls==[('GET','/internal/ict-shadow/candles?after=60')]


def test_activation_bound_to_approved_code_assets_and_expiry(tmp_path):
    import hashlib
    from services.ict_shadow.worker import verify_activation
    from services.tanja.ict_agent.common import digest
    assets=tmp_path/'assets.json';code=tmp_path/'code.json'
    assets.write_text('{"files":{}}');code.write_text('{"files":{}}')
    cfg={'release_manifest':str(assets)}
    a=dict(passive_only=True,orders_enabled=False,paid_calls_enabled=False,valid_from=10,expires_at=20,configuration_sha256=digest(cfg),assets_manifest_sha256=hashlib.sha256(assets.read_bytes()).hexdigest(),code_manifest_sha256=hashlib.sha256(code.read_bytes()).hexdigest())
    verify_activation(cfg,a,15,code)
    with pytest.raises(ValueError,match='CURRENT_PASSIVE_ACTIVATION'):verify_activation(cfg,a,20,code)
    code.write_text('{"files":{},"changed":true}')
    with pytest.raises(ValueError,match='RELEASE_MISMATCH'):verify_activation(cfg,a,15,code)
