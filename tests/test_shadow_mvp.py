import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path
import pytest
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.common import digest,encode
from services.tanja.ict_agent.shadow_mvp import initialize,append,assessment,audit,sync_latest
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.market import load_csv

def test_missing_llm_is_wait_and_missing_market_is_data_insufficient():
    s={'symbol':'NQ','state':'WAITING_FOR_CONFIRMATION','missing':['FVG']}
    assert assessment(s,{'NQ':{'stale':False}})[0]=='WAIT'
    assert assessment(s,{'NQ':{'stale':True}})[0]=='DATA_INSUFFICIENT'
    s.update(state='INVALIDATED',last_transition={'reason':'CLOSE_BEYOND_SWEEP_EXTREME'})
    assert assessment(s,{'NQ':{'stale':False}})[0]=='REJECTED'

def test_journal_append_idempotence_and_immutability(tmp_path):
    st=Store(tmp_path/'s.db',{});initialize(st)
    with st.connect() as db:
        append(db,'c',100,'ASSESSMENT',{'decision':'WAIT'})
        append(db,'c',100,'ASSESSMENT',{'decision':'WAIT'})
        with pytest.raises(ValueError,match='IMMUTABILITY'):
            append(db,'c',100,'ASSESSMENT',{'decision':'ACCEPTED'})
        assert db.execute('SELECT count(*) FROM shadow_records').fetchone()[0]==1
        for sql in ("UPDATE shadow_records SET stage='OTHER'",'DELETE FROM shadow_records'):
            with pytest.raises(sqlite3.IntegrityError,match='APPEND_ONLY'):db.execute(sql)

def test_future_evidence_and_execution_promotion_rejected(tmp_path):
    st=Store(tmp_path/'s.db',{});initialize(st)
    packet={'snapshot':{'as_of':100},'evidence':{'x':{'available_at':101}},'future_outcomes_included':False,'execution_permission':False}
    with st.connect() as db:db.execute('INSERT INTO shadow_evidence VALUES(?,?)',(digest(packet),encode(packet)))
    with pytest.raises(ValueError,match='LOOKAHEAD'):audit(st)
    second=Store(tmp_path/'other.db',{});initialize(second)
    with second.connect() as db:append(db,'c',100,'ASSESSMENT',{'source_qualified':True})
    with pytest.raises(ValueError,match='UNAUTHORIZED_QUALIFICATION'):audit(second)

def test_restart_recovers_committed_intake_before_shadow_projection(tmp_path):
    class K:
        version=index_version='UNIT_TEST_ONLY'
        verified=set()
        def retrieve(self,*a,**k):return {'records':[],'audit_notes':[]}
    bars,_=load_csv(Path(os.environ['ICT_TEST_DATA_DIR'])/'NQ.csv','NQ');bar=bars[0]
    st=Store(tmp_path/'s.db',{});initialize(st);Observer(st,K()).push([bar],emit=False)
    # Simulate a crash exactly between the existing Observer commit and new projection.
    reopened=Store(st.path)
    assert sync_latest(reopened,K(),bar['ts'],bar['received_at']) is True
    assert sync_latest(reopened,K(),bar['ts'],bar['received_at']) is False
    assert Observer(reopened,K()).push([bar],emit=False)['duplicate'] is True
    assert audit(reopened)['status']=='PASS'

def test_native_dashboard_readonly_and_library(tmp_path):
    from services.tanja.ict_agent.shadow_mvp_web import create_app
    st=Store(tmp_path/'s.db',{'session':'2026-05-20','start':1,'end':2,'coverage':{}});initialize(st)
    app=create_app(st.path,os.environ['ICT_TEST_LIBRARY'],tmp_path/'web')
    client=app.test_client()
    r=client.get('/ict-shadow?view=ictai')
    assert r.status_code==200
    assert b'shadow-mvp-panel' in r.data and b'ICT Knowledge Library' in r.data
    s=client.get('/ict-shadow/shadow-mvp/status').get_json()
    assert s['openai_calls']==s['broker_orders']==0
    assert s['semantic_status']=='PENDING_LLM'
    p=client.get('/ict-shadow/shadow-mvp/prospective-status').get_json()
    assert p['classification']=='UNVERIFIED' and p['prospective_candidates']==0 and not p['enabled']
    assert client.post('/ict-shadow/shadow-mvp/status').status_code==405
    assert client.get('/ict-shadow/shadow-mvp/candidates?offset=-1').status_code==400
    assert client.get('/ict-shadow/shadow-mvp/status',environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).status_code==403
    assert client.get('/ict-shadow/ai-trader/api/health').status_code==200
    assert not any('worker' in k.lower() for k in app.extensions)
