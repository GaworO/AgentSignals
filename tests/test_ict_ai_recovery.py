"""Recovery operates on local evidence only; worker/provider calls are mocked."""
import base64
import json
import sqlite3
import threading
from unittest.mock import Mock

import pytest
from flask import Flask
import ict_ai_recovery_web as recovery
import ict_shadow_cloud as cloudmod
from test_ict_shadow_cloud import ts


@pytest.fixture
def setup(tmp_path,monkeypatch):
    now=ts();monkeypatch.setattr(recovery.time,'time',lambda:now)
    monkeypatch.setattr(recovery.ict_news_context,'build',lambda *args:dict(status='CLEAR'))
    c=cloudmod.Cloud(dict(DATA_DIR=str(tmp_path),ICT_SHADOW_ENABLED='true',TANJA_DASHBOARD_PASSWORD='test-login'))
    c.put('worker',dict(at=now,enabled=True));c.put('market',dict(as_of=now-60))
    with sqlite3.connect(tmp_path/'ab_ai_context.sqlite3') as db:
        db.executescript('CREATE TABLE reviews(id TEXT,status TEXT,requested_at REAL,review_kind TEXT,finished REAL,error_code TEXT,day TEXT); CREATE TABLE setup_requests(state TEXT,expires REAL);')
        db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',('failed-original','unknown',now-3600,'SETUP',now-3500,'TimeoutError','2026-11-02'))
    c.start_worker=Mock(return_value='CHECK_REQUESTED')
    app=Flask(__name__)
    def private(fn):
        from flask import request,Response
        def wrapper():
            if request.headers.get('Authorization')!='Basic '+base64.b64encode(b'tanja:test-login').decode():return Response(status=401)
            return fn()
        return wrapper
    recovery.register(app,private,c)
    client=app.test_client();headers={'Authorization':'Basic '+base64.b64encode(b'tanja:test-login').decode()}
    return c,client,headers,now


def get_token(client,headers):
    r=client.get('/ict-shadow/ai-recovery',headers=headers,base_url=recovery.ORIGIN)
    assert r.status_code==200
    return r.json['csrf']


def post(client,headers,token):
    return client.post('/ict-shadow/ai-recovery/check',json={'recover':True},headers=dict(headers,Origin=recovery.ORIGIN,**{'X-ICT-Recovery-CSRF':token}),base_url=recovery.ORIGIN)


def test_get_is_authenticated_and_never_starts_worker(setup):
    c,client,h,_=setup
    assert client.get('/ict-shadow/ai-recovery').status_code==401
    token=get_token(client,h);assert len(token)>32
    assert not c.start_worker.called
    assert recovery.inspect(c)['state']=='READY'


def test_recovery_double_tap_does_not_reset_charge_or_production_attempt(setup):
    c,client,h,now=setup
    c.reserve('prior_0',dict(model=cloudmod.MODEL,service_tier='default',input='mock',max_output_tokens=5000),now-120)
    c.finish('prior_0',None,'timeout')
    with c.db() as db:before=[tuple(r) for r in db.execute('SELECT * FROM costs')]
    with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:production=db.execute('SELECT * FROM reviews').fetchall()
    token=get_token(client,h)
    assert post(client,h,token).json['action']=='CHECK_REQUESTED'
    assert post(client,h,token).json['action']=='ALREADY_REQUESTED'
    assert c.start_worker.call_count==1
    with c.db() as db:assert [tuple(r) for r in db.execute('SELECT * FROM costs')]==before
    with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:assert db.execute('SELECT * FROM reviews').fetchall()==production


@pytest.mark.parametrize('bad',['origin','token','body','auth','fetch_site'])
def test_recovery_rejects_cross_site_and_missing_confirmation(setup,bad):
    c,client,h,_=setup;token=get_token(client,h)
    headers=dict(h,Origin=recovery.ORIGIN,**{'X-ICT-Recovery-CSRF':token});body={'recover':True}
    if bad=='origin':headers['Origin']='https://other.example'
    if bad=='token':headers['X-ICT-Recovery-CSRF']='bad'
    if bad=='body':body={}
    if bad=='auth':headers.pop('Authorization')
    if bad=='fetch_site':headers['Sec-Fetch-Site']='cross-site'
    r=client.post('/ict-shadow/ai-recovery/check',json=body,headers=headers,base_url=recovery.ORIGIN)
    assert r.status_code in (400,401,403)
    assert not c.start_worker.called


@pytest.mark.parametrize('kind',['unknown','running','research_unknown','news','budget','outside','disabled','missing_db'])
def test_recovery_reports_real_blockers_and_does_not_clear_them(setup,monkeypatch,kind):
    c,client,h,now=setup
    if kind in ('unknown','running'):
        with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:db.execute('UPDATE reviews SET status=?,finished=NULL',(kind,))
    elif kind=='research_unknown':
        path=c.root/'ict_2022_nyam_20261102';path.mkdir()
        with sqlite3.connect(path/'research_budget.sqlite3') as db:
            db.execute('CREATE TABLE reviews(id TEXT,status TEXT,day TEXT)');db.execute('INSERT INTO reviews VALUES(?,?,?)',('ict_0',kind,'2026-11-02'))
    elif kind=='news':monkeypatch.setattr(recovery.ict_news_context,'build',lambda *args:dict(status='POLICY_BLOCKED',active_policy_reasons=['NFP_WEEK_AFTER_WEDNESDAY_1100']))
    elif kind=='budget':
        with c.db() as db:
            for i in range(16):db.execute('INSERT INTO costs VALUES(?,?,?,?,?,?)',('id_'+str(i),'2026-11-02',now,1,.1,'ESTIMATED'))
    elif kind=='outside':monkeypatch.setattr(recovery.time,'time',lambda:ts('2026-11-02T12:00:00'))
    elif kind=='disabled':c.config['ICT_SHADOW_ENABLED']='false'
    elif kind=='missing_db':(c.root.parent/'ab_ai_context.sqlite3').unlink()
    token=get_token(client,h);before=recovery.inspect(c)
    assert before['blockers']
    result=post(client,h,token)
    assert result.status_code==200
    assert recovery.inspect(c)['blockers']==before['blockers']
    if kind in ('disabled','missing_db'):assert not c.start_worker.called


def test_worker_restart_is_singleton_and_wake_does_not_call_tick(tmp_path,monkeypatch):
    c=cloudmod.Cloud(dict(DATA_DIR=str(tmp_path),ICT_SHADOW_ENABLED='true'))
    threads=[]
    class Thread:
        def __init__(self,**kw):self.alive=False;threads.append(self)
        def start(self):self.alive=True
        def is_alive(self):return self.alive
    monkeypatch.setattr(cloudmod.threading,'Thread',Thread)
    c.tick=Mock(side_effect=AssertionError('Direct paid path forbidden'))
    assert c.start_worker()=='WORKER_STARTED'
    assert c.start_worker()=='CHECK_REQUESTED'
    assert len(threads)==1 and c._wake.is_set()
    threads[0].alive=False
    assert c.start_worker()=='WORKER_STARTED' and len(threads)==2
    assert not c.tick.called


def test_worker_lock_still_excludes_competing_tick(tmp_path,monkeypatch):
    c=cloudmod.Cloud(dict(DATA_DIR=str(tmp_path),ICT_SHADOW_ENABLED='true'))
    import fcntl
    with (c.root/'ict_cloud.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        c.tick=Mock(side_effect=AssertionError('Competing worker must not enter'))
        c.pause=Mock(side_effect=RuntimeError('stop test loop'))
        with pytest.raises(RuntimeError,match='stop test loop'):c.run()
        assert not c.tick.called


@pytest.mark.parametrize('state',['THREE_REVIEWS_PER_30_MINUTES','90_SECOND_COOLDOWN','WATCHING_CANDLES_NO_NEW_EVENT','WAIT_FIRST_CLOSED_CANDLE','BLOCKED'])
def test_saved_scheduler_wait_is_visible_and_not_mislabelled_ready(setup,state):
    c,client,h,now=setup
    c.put('last_check',dict(at=now,state=state,reason='STALE_AT_SEND'))
    value=recovery.inspect(c)
    assert value['state']=='WAITING'
    timing=next(row for row in value['checks'] if row['label']=='Review timing')
    assert timing['state']=='wait' and timing['detail'] in value['blockers']
    token=get_token(client,h);post(client,h,token)
    assert recovery.inspect(c)['state']=='WAITING'
