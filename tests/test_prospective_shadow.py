"""Offline only: real archived prices; simulated receipt clocks are NOT live evidence."""
import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path
import pytest
from services.tanja.ict_agent.common import digest
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.prospective_shadow import PassiveObserver, gate, status
from services.tanja.ict_agent.shadow_mvp import audit


class K:
    version=index_version='OFFLINE_TEST_ONLY'
    verified=set()
    def retrieve(self,*args,**kwargs):return {'records':[],'audit_notes':[]}


@pytest.fixture
def batches():
    path=Path(os.environ['ICT_TEST_REPLAY_DB'])
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT payload FROM bars WHERE ts>=? AND ts<? ORDER BY ts,symbol',(1779228000,1779259800)).fetchall()
    result={}
    for raw, in rows:
        b=json.loads(raw);result.setdefault(b['ts'],[]).append(b)
    assert len(result)==530 and all(len(v)==3 for v in result.values())
    return list(result.values())


def make_observer(tmp_path,batches):
    clock=[(batches[0][0]['ts']+62)*10**9]
    st=Store(tmp_path/'observer.sqlite3',{'mode':'OFFLINE_ADAPTER_TEST','orders_enabled':False,'paid_calls_enabled':False})
    obs=PassiveObserver(st,K(),{},now_ns=lambda:clock[0])
    return obs,clock


def send(obs,clock,bars):
    clock[0]=(bars[0]['ts']+62)*10**9
    return obs.push(bars,{b['symbol']:b['received_at'] for b in bars})


def test_unverified_disabled_and_execution_flags_fail_closed(tmp_path):
    assert not gate({'classification':'UNVERIFIED'},100)['ready']
    st=Store(tmp_path/'s.db',{'mode':'PROSPECTIVE_SHADOW','orders_enabled':False,'paid_calls_enabled':False})
    with pytest.raises(ValueError,match='NOT_ENABLED'):PassiveObserver(st,K(),{})
    with pytest.raises(ValueError,match='NOT_VERIFIED'):PassiveObserver(st,K(),{},enabled=True)
    unsafe=Store(tmp_path/'unsafe.db',{'mode':'OFFLINE_ADAPTER_TEST','orders_enabled':True,'paid_calls_enabled':False})
    with pytest.raises(ValueError,match='NO_ORDER'):PassiveObserver(unsafe,K(),{})


def test_receipts_and_instruments_required(tmp_path,batches):
    obs,clock=make_observer(tmp_path,batches)
    try:
        bars=batches[0]
        with pytest.raises(ValueError,match='PAIRED'):obs.push(bars[:2],{})
        with pytest.raises(ValueError,match='EXACT_RECEIPTS'):obs.push(bars,{})
        bad={b['symbol']:b['received_at']-60 for b in bars}
        with pytest.raises(ValueError,match='RECEIPT_INCONSISTENT'):obs.push(bars,bad)
        assert status(obs.store)['durable_batches']==0
    finally:obs.close()


def test_real_archived_stream_journals_restarts_and_never_claims_live(tmp_path,batches):
    obs,clock=make_observer(tmp_path,batches)
    try:
        for bars in batches[:45]:send(obs,clock,bars)
        obs.close();obs=PassiveObserver(Store(obs.store.path),K(),{},now_ns=lambda:clock[0])
        for bars in batches[45:]:send(obs,clock,bars)
        assert status(obs.store)=={'mode':'OFFLINE_ADAPTER_TEST','durable_batches':530,'verified_prospective_batches':0,'unreceipted_intents':0,'llm_status':'PENDING_LLM','orders_enabled':False,'paid_calls_enabled':False}
        assert send(obs,clock,batches[-1])['duplicate']
        with obs.store.readonly() as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM shadow_records')]
            assert len([r for r in records if r['stage']=='DETECTED'])>0
            outcomes=[r for r in records if r['stage']=='OUTCOME'];assert outcomes
            for r in outcomes:
                assert r['forecast_as_of']<r['as_of']
                assert r['forecast_commit_observed_at_ns']<int(r['outcome_first_source_receipt']*1e9)
                assert r['prospective_outcome_eligible'] is False
                assert r['pnl'] is None and r['fill'] is None
            assert all(r['semantic_status']=='PENDING_LLM' and not r['execution_eligible'] and not r['source_qualified'] for r in records)
        assert audit(obs.store)['status']=='PASS'
    finally:obs.close()


@pytest.mark.parametrize('stage',['projection','receipt'])
def test_restart_after_partial_commit_never_backdates_timing(tmp_path,batches,stage):
    obs,clock=make_observer(tmp_path,batches)
    try:
        if stage=='projection':
            obs._project=lambda *args:(_ for _ in ()).throw(RuntimeError('TEST_CRASH'))
        else:
            original=obs._write
            def broken(kind,*args):
                if kind=='DURABLE':raise RuntimeError('TEST_CRASH')
                return original(kind,*args)
            obs._write=broken
        with pytest.raises(RuntimeError,match='TEST_CRASH'):send(obs,clock,batches[0])
        assert status(obs.store)['unreceipted_intents']==1
        obs.close();clock[0]+=600*10**9
        obs=PassiveObserver(Store(obs.store.path),K(),{},now_ns=lambda:clock[0])
        assert status(obs.store)['unreceipted_intents']==0
        with obs.store.readonly() as db:
            receipt=json.loads(db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE'").fetchone()[0])
            assert receipt['recovered_after_interruption'] and not receipt['prospective_timing_verified']
            assert receipt['commit_observed_at_ns']==clock[0]
            assert db.execute('SELECT count(*) FROM batches').fetchone()[0]==1
        assert obs.push(batches[0],{b['symbol']:b['received_at'] for b in batches[0]})['duplicate']
    finally:obs.close()


def test_append_only_concurrent_writer_and_conflicting_duplicate(tmp_path,batches):
    obs,clock=make_observer(tmp_path,batches)
    try:
        send(obs,clock,batches[0])
        with pytest.raises(BlockingIOError):PassiveObserver(Store(obs.store.path),K(),{},now_ns=lambda:clock[0])
        with obs.store.connect() as db:
            for sql in ("UPDATE passive_log SET key='x'",'DELETE FROM passive_log'):
                with pytest.raises(sqlite3.IntegrityError,match='APPEND_ONLY'):db.execute(sql)
        changed=deepcopy(batches[0]);changed[0]['open']=changed[0]['close']
        with pytest.raises(ValueError,match='CONFLICTING_DUPLICATE'):send(obs,clock,changed)
    finally:obs.close()


def test_late_bar_cannot_rewrite_previous_decision(tmp_path,batches):
    obs,clock=make_observer(tmp_path,batches)
    try:
        send(obs,clock,batches[1]);clock[0]+=10**9
        with pytest.raises(ValueError,match='NO_RETROSPECTIVE_REWRITE'):
            obs.push(batches[0],{b['symbol']:b['received_at'] for b in batches[0]})
        with obs.store.readonly() as db:
            assert db.execute('SELECT count(*) FROM bars').fetchone()[0]==3
            assert db.execute("SELECT count(*) FROM passive_log WHERE kind='LATE_EXCLUDED'").fetchone()[0]==1
    finally:obs.close()


def test_mock_entitlement_calendar_binding_expiry_and_catchup(tmp_path,batches):
    # This is a MOCK proof, explicitly NOT a verification of the real feed.
    import hashlib
    mock=tmp_path/'MOCK_NOT_ENTITLEMENT.txt';mock.write_text('offline fixture')
    bars=deepcopy(batches[0]);t=bars[0]['ts'];clock=[(t+62)*10**9]
    for b in bars:b['source']='TANJA_READ_ONLY_RECEIPT_TIME'
    proof={'verified':True,'valid_from':t,'valid_until':t+3600,'evidence_file':str(mock),'sha256':hashlib.sha256(mock.read_bytes()).hexdigest()}
    report={'classification':'VERIFIED_REALTIME','symbols':['NQ','MNQ','ES'],'entitlement':dict(proof),'instrument_binding':dict(proof,tickers={b['symbol']:b['contract'] for b in bars}), 'exchange_calendar':dict(proof,open_intervals_utc=[[t,t+3600]])}
    st=Store(tmp_path/'mock.db',{'mode':'PROSPECTIVE_SHADOW','feed_report_sha256':digest(report),'orders_enabled':False,'paid_calls_enabled':False})
    obs=PassiveObserver(st,K(),report,enabled=True,now_ns=lambda:clock[0])
    try:
        wrong=deepcopy(bars);wrong[0]['contract']='WRONG'
        with pytest.raises(ValueError,match='BINDING'):obs.push(wrong,{b['symbol']:b['received_at'] for b in wrong})
        r=obs.push(bars,{b['symbol']:b['received_at'] for b in bars})
        assert r['prospective_timing_verified'] # simulated clock only
        more=deepcopy(batches[1])
        for b in more:b['source']='TANJA_READ_ONLY_RECEIPT_TIME'
        clock[0]=(t+600)*10**9
        r=obs.push(more,{b['symbol']:b['received_at'] for b in more})
        assert r['mode']=='CATCHUP_REPLAY' and not r['prospective_timing_verified']
        clock[0]=(t+3600)*10**9
        with pytest.raises(ValueError,match='EXPIRED'):obs.push(more,{b['symbol']:b['received_at'] for b in more})
        mock.write_text('changed')
        assert not gate(report,t+100)['ready']
    finally:obs.close()


def test_readonly_bridge_preserves_receipts_and_records_all_symbol_gap(tmp_path,batches,monkeypatch):
    import hashlib
    from tools import run_passive_shadow as runner
    source=tmp_path/'source.db'
    with sqlite3.connect(source) as db:
        for name in ('bars','collection_bars'):
            db.execute('CREATE TABLE '+name+'(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
        for bars in (batches[0],batches[2]):
            for b in bars:
                p=dict(symbol=b['symbol'],ticker=b['contract'],confirmed=True,standard=True,bar_open_ms=b['ts']*1000,bar_close_ms=(b['ts']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                table='collection_bars' if b['symbol']=='NQ' else 'bars'
                db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?)',(b['symbol'],b['ts'],b['ts']+60,b['ts']+60.25,json.dumps(p)))
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    obs,clock=make_observer(tmp_path/'observer',batches)
    try:
        clock[0]=(batches[2][0]['ts']+125)*10**9
        obs.report={'exchange_calendar':{'open_intervals_utc':[[batches[0][0]['ts'],batches[2][0]['ts']+60]]}}
        monkeypatch.setattr(runner.time,'time_ns',lambda:clock[0])
        result=runner.poll_once(obs,source,clock[0]/1e9)
        assert result['processed']==2
        assert {'ts':batches[1][0]['ts'],'missing':['ES','MNQ','NQ']} in result['missing_pairs']
        with obs.store.readonly() as db:
            intents=[json.loads(r[0]) for r in db.execute("SELECT payload FROM passive_log WHERE kind='INTENT'")]
            assert all(set(i['source_receipts'].values())=={i['bars'][0]['ts']+60.25} for i in intents)
            assert db.execute("SELECT count(*) FROM events WHERE kind='DATA_GAP'").fetchone()[0]==3
        assert runner.poll_once(obs,source,clock[0]/1e9)['processed']==0
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    finally:obs.close()
