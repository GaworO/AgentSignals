"""Focused release checks. Synthetic integration data, not trading outcomes."""
import fcntl
import json
from copy import deepcopy
import pytest
from flask import Flask
from services.tanja.ict_agent.common import digest, encode
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.integrated_web import blueprint
from services.tanja.ict_agent.ifvg_observation import observe
from tools.advance_ifvg_observe_manifest import advance
from test_ict_incremental_ifvg import NoSourceHints, bars, memory


def test_api_round_trip_and_code_handoff_preserves_all_observations(tmp_path):
    path=tmp_path/'observer.sqlite3'
    manifest=dict(code_hash='old',mode='READ_ONLY_OBSERVER',orders_enabled=False,
                  intake_policy=dict(kind='BOUNDED_RECENT_WINDOW',max_age_seconds=300,
                                     max_batches_per_poll=5,older_history='NOT_REPLAYED'))
    store=Store(path,manifest);observer=Observer(store,NoSourceHints())
    inputs=list(bars())
    for b in inputs:observer.push([b],force_snapshot=True)
    snap=store.snapshot();before=memory(store)
    def contents():
        with store.readonly() as db:
            return {t:db.execute('SELECT * FROM '+t+' ORDER BY 1').fetchall()
                    for t in ('memory','snapshots','events','bars','batches','frames','measurements')}
    original=contents()
    result=advance(path,tmp_path/'lock','old','new',guard=lambda:None)
    assert result['changed_fields']==['code_hash'] and contents()==original
    restarted=Observer(Store(path,dict(manifest,code_hash='new')),NoSourceHints())
    assert restarted.push([inputs[-1]])['duplicate'] and contents()==original
    assert memory(store)==before
    with store.readonly() as db:
        assert json.loads(db.execute('SELECT payload FROM metadata WHERE key=?',('manifest:'+snap['run_manifest_hash'],)).fetchone()[0])==manifest
    app=Flask(__name__);app.register_blueprint(blueprint({'ICT_AI_OBSERVE_DB':str(path),'DATA_DIR':str(tmp_path)},lambda:True),url_prefix='/ict-shadow/ai-trader')
    client=app.test_client()
    got=client.get('/ict-shadow/ai-trader/api/snapshots/'+snap['id']+'?mode=observe')
    assert got.status_code==200 and got.json==snap
    assert got.json['ifvg_assessment']['action']=='UNKNOWN'
    assert any(e['kind']=='IFVG_RETEST_OBSERVED' and e['fill_verified'] is False for e in got.json['events'])
    state=client.get('/ict-shadow/ai-trader/api/state?mode=observe').json
    assert state['orders_enabled'] is False and state['paid_calls_enabled'] is False


def test_handoff_requires_exclusive_lock_and_exact_previous_code(tmp_path):
    path=tmp_path/'observer.sqlite3';store=Store(path,{'code_hash':'old'})
    with (tmp_path/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):advance(path,tmp_path/'lock','old','new',guard=lambda:None)
    with pytest.raises(ValueError,match='EXPECTED_OLD_MANIFEST_REQUIRED'):
        advance(path,tmp_path/'lock','wrong','new',guard=lambda:None)
    with store.readonly() as db:
        assert db.execute('SELECT count(*) FROM metadata').fetchone()[0]==1


def test_es_april13_m5_to_m1_close_and_later_retest_regression():
    # Real archive excerpt: relative 09:00 NY. Zone already known at 09:20.
    # Missing intervening candles are not invented: start tracking at 10:06,
    # and explicitly record incomplete history from formation.
    zone=dict(id='ES-M5-original',kind='FVG',direction='SHORT',timeframe=5,
              symbol='ES',contract='archive-id:95414',lower=4131.5,upper=4132.25,
              available_at=20*60)
    markets={'ES':{'zones':[zone]}};tracked={}
    prices=[(65,4131.5,4130.75,4134.25),(66,4132.25,4131.25,4133),
            (67,4134.75,4132,4134.75),(68,4135,4134,4136),(69,4134.5,4133,4135)]
    events=[]
    for minute,close,low,high in prices:
        frame=dict(id='ES-M1-'+str(minute),symbol='ES',contract='archive-id:95414',timeframe=1,
                   ts=minute*60,end=(minute+1)*60,available_at=(minute+1)*60,close=close,low=low,high=high)
        new=observe(tracked,markets,[frame],frame['available_at']);events+=new
        if minute<=66:assert not new
    assert [(e['kind'],e['available_at']) for e in events]==[('IFVG_INVERSION_OBSERVED',68*60)]
    assert events[0]['zone_timeframe']==5 and events[0]['execution_timeframe']==1
    assert tracked['ES-M5-original:M1']['first_retest'] is None
    assert not tracked['ES-M5-original:M1']['history_complete_from_formation']


def test_existing_m5_zone_is_not_backfilled_when_tracking_begins_between_closes():
    z=dict(id='z',kind='FVG',direction='SHORT',timeframe=5,symbol='NQ',contract='NQZ2026',lower=99,upper=100,available_at=0)
    tracked={};markets={'NQ':{'zones':[z]}}
    def f(tf,start,close):return dict(id=str(tf)+':'+str(start),timeframe=tf,ts=start,end=start+tf*60,available_at=start+tf*60,symbol='NQ',contract='NQZ2026',low=98,high=102,close=close)
    assert not observe(tracked,markets,[f(1,420,99)],480)
    assert not observe(tracked,markets,[f(1,480,99)],540)
    out=observe(tracked,markets,[f(1,540,101),f(5,300,101)],600)
    assert {e['execution_timeframe'] for e in out}=={1,5}
    assert all(e['available_at']==600 for e in out)
