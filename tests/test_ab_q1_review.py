"""Only mocked AI and temporary SQLite; no production calls or broker writes."""
import copy
import json
from datetime import datetime
from unittest.mock import Mock

import pytest
import ab_quality
import ab_q1_review as q1
import ab_ai_context as ai
import directional_ai_review as directional
from test_directional_hybrid import h, candidate as directional_candidate, mocked_response
from test_directional_setup_review import service


def signal(side='LONG'):
    s=dict(_strat='A/B',model='Cont',cat='NDOG',dir=side,entry=100.,
           SL=90. if side=='LONG' else 110.,TP=130. if side=='LONG' else 70.,
           s=10,u=14,bos_bar=17,bos_ms=1792418340000,
           signal_close=102. if side=='LONG' else 98.,fvg_lo=99.,fvg_hi=102.,atr5=4.)
    s['_ab_quality']=ab_quality.classify(s)
    assert s['_ab_quality']['tier']=='Q1'
    return s


def order(h,side='LONG',key='q-one'):
    h.config.update(AB_Q1_AI_REVIEW_ENABLED='true',AB_Q1_AI_SESSIONS=q1.DEFAULT_SESSIONS)
    h.support.config.update(h.config)
    s=signal(side);s['bos_ms']=int(h.now)//60*60000-60000
    return q1.candidate(s,key,h.config)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_exact_q1_allow_shared_worker(h,side):
    o=order(h,side);assert h.support.queue_setup(o)['state']=='queued'
    assert h.support.queue_setup(o)['state']=='queued'
    assert not h.calls
    assert h.support.process_one()
    reason,r=q1.check(o,h.support)
    assert reason is None and r['owner']=='A/B' and r['executable'] is False
    assert directional.check(side,o['order_id'])[0]=='v3_ai:owner_mismatch'
    p=json.loads(h.calls[0]['input'][1]['content'])
    assert p['owner']=='A/B' and p['setup']['strategy']=='A/B'
    assert p['setup']['target_r']==3 and p['setup']['quality']['tier']=='Q1'
    assert 'original supplied target' in h.calls[0]['input'][0]['content']
    with h.support.connect() as db:
        r=db.execute('SELECT * FROM reviews').fetchone()
        assert r['review_kind']=='SETUP' and r['prompt_version']==q1.VERSION
        assert r['response_text']


@pytest.mark.parametrize('kind',['WAIT','REJECT','timeout','late','wrong_geometry','opposing'])
def test_no_approval_from_failed_q1_reviews(h,kind):
    o=order(h)
    def transport(payload):
        if kind=='timeout':raise TimeoutError()
        raw=mocked_response(h,payload);d=json.loads(raw['output'][0]['content'][0]['text'])
        if kind in ('WAIT','REJECT'):d['long_scenario']['permission']=kind
        if kind=='wrong_geometry':d['approved_geometry']['entry_price']+=.25
        if kind=='late':h.now+=121
        if kind=='opposing':
            h.now+=60
            with h.support.connect() as db:db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',('opp',h.now,json.dumps(dict(symbol='MNQ',kind='MSS',direction='DOWN'))))
        raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
    h.support.transport=transport;h.support.queue_setup(o);h.support.process_one()
    assert q1.check(o,h.support)[0] is not None


def test_q1_cannot_reuse_directional_review(h):
    o=order(h);h.support.queue_setup(directional_candidate(h));h.support.process_one()
    assert q1.check(o,h.support)[0] is not None


def test_mutated_geometry_revokes_q1_review(h):
    o=order(h);h.support.queue_setup(o);h.support.process_one()
    assert q1.check(o,h.support)[0] is None
    changed=dict(o,target_price=140.)
    assert q1.check(changed,h.support)[0]=='q1_ai_setup_changed'
    assert h.support.queue_setup(changed)['state']=='SETUP_CHANGED'
    assert q1.check(o,h.support)[0]=='q1_ai:superseded'


def test_disabled_by_default_and_q0_q2_not_queued(h):
    assert q1.queue_signal(signal(),'x',h.support)['state']=='DISABLED'
    h.support.config['AB_Q1_AI_REVIEW_ENABLED']='true'
    for update in [dict(signal_close=110.),dict(u=15)]:
        s=signal();s.update(update);s['_ab_quality']=ab_quality.classify(s)
        assert s['_ab_quality']['tier']!='Q1'
        assert q1.queue_signal(s,'x',h.support)['state']=='INELIGIBLE'
    s=signal();s['_ab_quality']['checks']['disp_len_ge_6']=True
    assert q1.queue_signal(s,'x',h.support)['state']=='INELIGIBLE'
    assert h.calls==[]


@pytest.mark.parametrize('stamp,name',[
 ('2026-10-28T01:59','ASIA'),('2026-10-28T02:00',None),
 ('2026-10-28T05:00','PREM'),('2026-10-28T09:30','NYAM'),
 ('2026-10-28T11:00',None),('2026-10-28T13:30','NYPM'),
 ('2026-10-28T16:00',None),('2026-10-28T18:00','ASIA'),
 ('2026-10-30T18:00',None),('2026-11-01T18:00','ASIA'),
 ('2026-11-02T01:30','ASIA'),('2026-11-02T05:00','PREM')])
def test_q1_sessions_ny_dst_and_weekend(stamp,name):
    r=q1.session_at(datetime.fromisoformat(stamp).replace(tzinfo=q1.NY).timestamp(),{})
    assert (r['name'] if r else None)==name


def test_asia_q1_review_does_not_enable_directional_asia(h):
    h.now=datetime.fromisoformat('2026-10-28T19:07').replace(tzinfo=q1.NY).timestamp()
    o=order(h);assert h.support.session_at(h.now) is None
    assert h.support.queue_setup(o)['state']=='queued';assert h.support.process_one()
    assert q1.check(o,h.support)[0] is None
    packet=json.loads(h.calls[0]['input'][1]['content'])
    assert packet['evidence']['AB_Q1:'+o['order_id']+':session']['value']['session']=='ASIA'
    assert h.support.queue_setup(directional_candidate(h))['state']=='OUTSIDE_AB_SESSIONS'


def test_shared_seven_setup_slots_and_directional_priority(h):
    o=order(h);h.support.queue_setup(o);d=directional_candidate(h)
    h.support.queue_setup(d);h.support.process_one()
    assert json.loads(h.calls[0]['input'][1]['content'])['setup']['strategy']=='AB_DIRECTIONAL'
    h.support.process_one()
    for i in range(5):
        h.support.queue_setup(order(h,key=str(i)));h.support.process_one()
    assert len(h.calls)==7
    last=order(h,key='eighth');h.support.queue_setup(last)
    assert not h.support.process_one();assert h.support.status=='SETUP_ALLOCATION_EXHAUSTED'
    assert q1.check(last,h.support)[0]=='q1_ai:budget_blocked'


def test_restart_keeps_request_single_attempt(h):
    o=order(h);h.support.queue_setup(o)
    def crash(payload):raise KeyboardInterrupt()
    h.support.transport=crash
    with pytest.raises(KeyboardInterrupt):h.support.process_one()
    mock=Mock(side_effect=AssertionError('must not retry'))
    fresh=ai.ContextSupport(h.config,reader=h.source,transport=mock,clock=lambda:h.now)
    assert not fresh.process_one();mock.assert_not_called()
    assert q1.check(o,fresh)[0] is not None


def test_q1_payload_has_own_evidence_namespace_and_no_outcomes(h):
    order(h)
    s=signal();s.update(net_pnl=999,win=True,future_candles=['do not include'])
    o=q1.candidate(s,'clean',h.config)
    h.support.queue_setup(o);h.support.process_one()
    packet=json.loads(h.calls[0]['input'][1]['content'])
    assert any(k.startswith('AB_Q1:') for k in packet['evidence'])
    assert not any(k.startswith('DIRECTIONAL:ABQ1_') for k in packet['evidence'])
    for field in ('net_pnl','future_candles','"win"'):assert field not in json.dumps(packet)
    with h.support.connect() as db:
        obs=json.loads(db.execute('SELECT payload FROM setup_review_observations').fetchone()[0])
        assert obs['executable'] is False
        assert obs['blocking_reason']=='Q1_SHARED_EXECUTION_ROUTE_NOT_IMPLEMENTED'


def test_existing_directional_only_broker_permission_is_unchanged(monkeypatch):
    import execution_policy
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    assert execution_policy.shadow_family('A/B') is True
    assert execution_policy.shadow_family('AB_DIRECTIONAL') is False


@pytest.mark.parametrize('changes',[dict(recovery_version=2),dict(parent_opportunity_id='old'),dict(nofill_recovery_allowed=True),dict(maximum_entry_attempts=2)])
def test_q1_recovery_cannot_be_requested(h,changes):
    o=order(h);o.update(changes)
    with pytest.raises(ValueError,match='q1_recovery_not_permitted'):
        q1.describe(o,h.config['AB_V3_CONTRACT'])
    assert h.calls==[]


def test_q1_ends_at_original_expiry_with_no_paid_rereview(h):
    o=order(h);h.support.queue_setup(o);h.support.process_one()
    assert q1.check(o,h.support)[0] is None
    h.now=o['expiry_ms']/1000
    assert h.support.queue_setup(o)['state']=='EXPIRED'
    assert q1.check(o,h.support)[0] is not None
    with h.support.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM nofill_opportunities').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM reviews WHERE review_kind='SETUP'").fetchone()[0]==1
    assert len(h.calls)==1
