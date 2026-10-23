"""Causal replay / mocked AI only; no real broker or paid calls."""
import copy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import directional_management_context as context
import directional_management_review as review
import directional_hybrid_context as hybrid
from test_directional_position_manager import setup
from test_directional_setup_review import service


def bars(m,*,now=120000):
    with m.connect() as db:
        db.execute('CREATE TABLE bars(tf TEXT,contract TEXT,ts_ms INTEGER,bar_json TEXT,received_ms INTEGER)')
        for t in range(0,3600000,60000):
            b=dict(open=100,high=102,low=99,close=101,volume=1)
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1','MNQZ2026',t,json.dumps(b),t+60000))
    return SimpleNamespace(connect=m.connect)


def test_closed_complete_aggregates_no_future_or_receipt_lookahead(tmp_path):
    m,b,a=setup(tmp_path);api=bars(m)
    p=context.packet_from_bars(api,'MNQZ2026',900000)
    assert {e['timeframe'] for e in p['evidence'].values()}=={1,5,15}
    assert all(e['available_at']<=900 for e in p['evidence'].values())
    with m.connect() as db:db.execute('UPDATE bars SET received_ms=9999999 WHERE ts_ms=0')
    p=context.packet_from_bars(api,'MNQZ2026',900000)
    assert not any(e['timeframe']==15 for e in p['evidence'].values())
    assert len([e for e in p['evidence'].values() if e['timeframe']==5])==2


def test_monitor_is_observational_and_restart_persistent(tmp_path):
    m,b,a=setup(tmp_path);api=bars(m)
    s=context.monitor(m,api,'one',120000)
    assert s['market_monitor']['position_mark']['basis']=='CLOSED_M1_OBSERVATION_NOT_EXECUTABLE'
    assert s['market_monitor']['context']['es_nq_alignment']['status']=='UNKNOWN'
    assert s['unrealized_r'] is None
    assert m.get('one')['market_monitor']==s['market_monitor']
    assert not a.sent
    s=context.monitor(m,api,'one',9999999)
    assert s['market_monitor']['status']=='UNKNOWN'


def position_fixture(tmp_path,service):
    m,b,a=setup(tmp_path);now=int(service.now*1000)
    s=m.get('one');s['observed_ms']=now;s['fills']['101']['ms']=now-600000
    s['original_policy_decision']=dict(decision='CLOSE',known_ms=now,reason='EXISTING_RULE_FIXTURE')
    packet=dict(as_of=int(service.now),evidence={},source_packet_id='recorded-fixture')
    s['market_monitor']=dict(status='OBSERVATIONAL',as_of_ms=now,packet=packet,position_mark={},context={'structure_events':{'value':[]}})
    with m.connect() as db:m.save(db,s)
    support=service.support;support.trigger='HYBRID';support.owner='AB_DIRECTIONAL'
    from datetime import datetime
    import ab_ai_context as ai
    support.source_usage=dict(day=datetime.fromtimestamp(service.now,ai.NY).date().isoformat(),calls=0)
    def reader():return m,[m.get('one')]
    def transport(payload):
        p=json.loads(payload['input'][1]['content'])
        d=dict(setup_id='one',position_fingerprint=p['position']['fingerprint'],action='EXIT',reasoning='Mock only',evidence_ids=[],missing=['Independent broker proof'])
        return dict(status='completed',model='mock',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])
    support.transport=Mock(side_effect=transport)
    return m,support,reader,transport


@pytest.mark.parametrize('case',['valid','timeout','expired','changed_position','changed_context','wrong_identity','crash'])
def test_position_review_persistence_binding_no_execution(tmp_path,service,case):
    m,s,reader,transport=position_fixture(tmp_path,service)
    def call(p):
        if case=='timeout':raise TimeoutError()
        if case=='crash':raise KeyboardInterrupt()
        raw=transport(p)
        if case=='expired':service.now+=121
        if case.startswith('changed_'):
            x=m.get('one')
            if case=='changed_position':x['broker_fingerprint']='other'
            else:x['market_monitor']['packet']['source_packet_id']='new'
            with m.connect() as db:m.save(db,x)
        if case=='wrong_identity':
            d=json.loads(raw['output'][0]['content'][0]['text']);d['setup_id']='other';raw['output'][0]['content'][0]['text']=json.dumps(d)
        return raw
    s.transport=Mock(side_effect=call)
    if case=='crash':
        with pytest.raises(KeyboardInterrupt):review.process(s,states_reader=reader)
    else:assert review.process(s,states_reader=reader)
    with s.connect() as db:
        row=db.execute('SELECT * FROM reviews').fetchone()
        assert row['review_kind']=='POSITION' and row['request'] and row['prompt_version']==review.VERSION
        assert row['status']=={'valid':'validated','timeout':'unknown','expired':'expired','changed_position':'invalidated','changed_context':'invalidated','wrong_identity':'rejected','crash':'running'}[case]
    # Review never generates execution intents; restart does not repeat paid IO.
    with m.connect() as db:assert db.execute('SELECT COUNT(*) FROM ict_management_intents').fetchone()[0]==0
    review.process(s,states_reader=reader)
    assert s.transport.call_count==1


def test_position_budget_preserves_setup_capacity():
    rows=[]
    for kind in ['POSITION']*2+['CONTEXT']*3+['SETUP']*7:
        assert hybrid.budget_reason(rows,0,kind) is None
        rows.append({'review_kind':kind})
    assert hybrid.budget_reason(rows,0,'POSITION')=='DAILY_LIMIT_REACHED'
    assert hybrid.budget_reason([{'review_kind':'POSITION'}]*2,0,'POSITION')=='POSITION_ALLOCATION_EXHAUSTED'
    assert hybrid.budget_reason([],5,'POSITION')=='RESERVED_CAPACITY_PROTECTED'


def test_queued_setup_has_priority(tmp_path,service):
    m,s,reader,transport=position_fixture(tmp_path,service)
    with s.connect() as db:db.execute('INSERT INTO setup_requests VALUES(?,?,?,?,?,?,?)',('entry','hash',service.now,service.now+60,'queued','{}',None))
    assert not review.process(s,states_reader=reader)
    s.transport.assert_not_called()


@pytest.mark.parametrize('action',['HOLD','REDUCE','EXIT'])
def test_ai_action_is_advice_never_deterministic_order(tmp_path,service,action):
    m,s,reader,transport=position_fixture(tmp_path,service)
    def call(payload):
        raw=transport(payload);d=json.loads(raw['output'][0]['content'][0]['text']);d['action']=action
        raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
    s.transport=call
    assert review.process(s,states_reader=reader)
    advice=m.get('one')['position_ai_advice']
    assert advice['decision']['action']==action and advice['authority']=='ADVISORY_ONLY_NO_ACTION_PERMISSION'
    with m.connect() as db:assert db.execute('SELECT COUNT(*) FROM ict_management_intents').fetchone()[0]==0
