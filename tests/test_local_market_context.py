import base64
import json
import time
from types import SimpleNamespace

import pytest
from flask import Flask
import local_market_context as local
from local_context_engine.review_calendar import review_day

TOKEN='m'*48

def config(tmp_path):
    return dict(DATA_DIR=str(tmp_path),MARKET_CONTEXT_ENABLED='true',MARKET_CONTEXT_FEED_TOKEN=TOKEN,AB_CONTEXT_SOURCE_PASSWORD='p'*32)

def bar(symbol,start):
    return dict(schema_version=1,feed_version='tanja-1m-v1',symbol=symbol,ticker='CME_MINI:'+symbol+'1!',timeframe='1',session='regular',standard=True,confirmed=True,bar_open_ms=start*1000,bar_close_ms=(start+60)*1000,sent_at_ms=(start+60)*1000,open=100,high=102,low=99,close=101,volume=100)

def test_disabled_register_has_no_worker(tmp_path):
    app=Flask(__name__)
    assert local.register(app,{'DATA_DIR':str(tmp_path)},start=False) is None
    assert not list(tmp_path.iterdir())

def test_intake_requires_auth_all_three_and_closed_candles(tmp_path):
    app=Flask(__name__);instance=local.register(app,config(tmp_path),start=False);client=app.test_client()
    start=int(time.time()//60)*60-60
    assert client.post('/feed/wrong',json=bar('ES',start)).status_code==404
    for s in ('ES','NQ'):
        r=client.post('/feed/'+TOKEN,json=bar(s,start));assert r.status_code==200
        assert not r.json['queued']
    r=client.post('/feed/'+TOKEN,json=bar('MNQ',start));assert r.json['queued']
    assert instance.state()['jobs']=={'queued':1}
    assert client.post('/feed/'+TOKEN,json=bar('MNQ',start)).json['status']=='duplicate'
    changed=bar('MNQ',start);changed['close']=100
    assert client.post('/feed/'+TOKEN,json=changed).status_code==409
    for mutate in (lambda b:b.update(confirmed=False),lambda b:b.update(ticker='CME_MINI:NQ1!'),lambda b:b.update(bar_close_ms=(start+120)*1000)):
        b=bar('ES',start);mutate(b);assert client.post('/feed/'+TOKEN,json=b).status_code==422
    assert client.post('/feed/'+TOKEN,json=bar('ES',start-600)).status_code==422

def test_source_calendar_no_network_no_ai(tmp_path):
    instance=local.LocalContext(config(tmp_path));now=1794078000
    s=instance.source(now)
    assert s['ai_status']=='DISABLED' and s['calls_today']==0
    assert s['calendar']==review_day(now)
    assert s['context'] is None
    assert instance.state()['ai_worker'] is False and instance.state()['broker_worker'] is False

def test_snapshot_retention_and_restart(tmp_path):
    instance=local.LocalContext(config(tmp_path))
    for i in range(22):
        with instance.store.connect() as db:db.execute("insert into nq_jobs(cutoff,frozen_at,status) values(?,?,'processing')",(i*60,i*60))
        instance.store.finish({'cutoff':i*60},{'packet':{'as_of':i*60}},[],i*60)
    with instance.store.connect() as db:
        assert db.execute('select count(*) from nq_jobs where packet is not null').fetchone()[0]==16
        db.execute("insert into nq_jobs(cutoff,frozen_at,status) values(9999,9999,'processing')")
    second=local.LocalContext(config(tmp_path));second.store.recover()
    assert second.state()['jobs']['queued']==1
    assert second.source()['context']['packet']['as_of']==21*60

def test_source_mode_is_explicit_no_remote_fallback(monkeypatch):
    import ab_ai_context
    monkeypatch.setattr(local,'INSTANCE',None)
    with pytest.raises(ValueError,match='not_started'):
        ab_ai_context.ContextSupport.read_source(SimpleNamespace(config={'AB_CONTEXT_SOURCE':'LOCAL'}))
    with pytest.raises(ValueError,match='unknown_market_context_source'):
        ab_ai_context.ContextSupport.read_source(SimpleNamespace(config={'AB_CONTEXT_SOURCE':'TYPO'}))

def test_state_auth_and_symbol_identity(tmp_path):
    app=Flask(__name__);instance=local.register(app,config(tmp_path),start=False)
    c=app.test_client();assert c.get('/market-data/state').status_code==401
    auth='Basic '+base64.b64encode(('tanja:'+'p'*32).encode()).decode()
    s=c.get('/market-data/state',headers={'Authorization':auth}).json
    assert set(s['feeds'])=={'ES','NQ','MNQ'}
    cfg=config(tmp_path);cfg['MARKET_CONTEXT_ES_TICKER']='CME_MINI:ESZ2026'
    with pytest.raises(ValueError,match='Ticker configuration differs'):local.LocalContext(cfg)

def test_paid_calls_and_scanners_not_imported_in_isolated_runtime(tmp_path):
    import subprocess,sys
    code="import sys;import local_market_context;assert not {'ai_review','transcript_ingestion','selection_observer','trading_loop','broker_feedback','setup_scanner'}.intersection(sys.modules)"
    subprocess.run([sys.executable,'-c',code],check=True)

def test_dated_reference_kept_separately_without_execution(tmp_path):
    cfg=config(tmp_path);cfg['MARKET_CONTEXT_REFERENCE_CONTRACTS']=json.dumps({s:s+'Z2026' for s in ('ES','NQ','MNQ')})
    app=Flask(__name__);inst=local.register(app,cfg,start=False);c=app.test_client()
    start=int(time.time()//60)*60-60
    bars=[bar(s,start) for s in ('ES','NQ','MNQ')]
    for b in bars:b['ticker']='CME_MINI:'+b['symbol']+'Z2026'
    event=dict(version='tanja-dated-reference-v1',bars=bars)
    r=c.post('/reference/'+TOKEN,json=event)
    assert r.status_code==200 and not r.json['used_for_execution']
    assert inst.state()['jobs']=={} and all(v['bars']==0 for v in inst.state()['feeds'].values())
    assert c.post('/reference/'+TOKEN,json=event).status_code==200
    event['bars'][1]['close']=100
    assert c.post('/reference/'+TOKEN,json=event).status_code==409
    with inst.store.connect() as db:assert db.execute('select count(*) from reference_observations').fetchone()[0]==3

def test_future_or_late_arrival_never_rewrites_frozen_history(tmp_path):
    inst=local.LocalContext(config(tmp_path));t=1794000000
    inst.store.ingest(bar('ES',t),t+61)
    inst.store.ingest(bar('ES',t+60),t+121)
    inst.store.ingest(bar('ES',t-60),t+130)
    history=inst.store.history('ES',t+60,t+70)
    assert [b['bar_open_ms'] for b in history]==[t*1000]
