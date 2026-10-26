"""Session-open context + requested reviews; mocked AI only, no broker I/O."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import ab_ai_context as ai
import directional_hybrid_lifecycle as life
from test_directional_hybrid import h, candidate
from test_directional_setup_review import service,order

CONFIG=dict(AB_AI_OWNER='AB_DIRECTIONAL',SKIP_SESSIONS='ASIA,LO,NYL',MONDAY_MODE='nyam')
def at(s):return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()

@pytest.mark.parametrize('time,name',[
 ('04:59',None),('05:00','PREM'),('08:04','PREM'),('09:29','PREM'),
 ('09:30','NYAM'),('11:00',None),('13:29',None),('13:30','NYPM'),
 ('15:59','NYPM'),('16:00','PM_AH'),('18:00',None)])
def test_configured_review_boundaries(time,name):
    s=ai.session_at(at('2026-10-27T'+time),CONFIG)
    assert (s['name'] if s else None)==name


def test_monday_exception_weekend_and_skipped_sessions_remain_enforced():
    assert ai.session_at(at('2026-10-26T08:04'),CONFIG) is None
    assert ai.session_at(at('2026-10-26T08:04'),dict(CONFIG,MONDAY_MODE='full'))['name']=='PREM'
    assert ai.session_at(at('2026-10-26T10:00'),dict(CONFIG,MONDAY_MODE='skip')) is None
    assert ai.session_at(at('2026-10-31T10:00'),CONFIG) is None
    assert ai.session_at(at('2026-10-27T08:04'),dict(CONFIG,SKIP_SESSIONS='PREM')) is None

@pytest.mark.parametrize('day',['2026-10-27','2026-11-03'])
def test_dst_and_expiry_use_new_york_session_end(day):
    t=at(day+'T09:29')
    assert ai.expires_at(t,60,CONFIG)==at(day+'T09:30')


def test_queued_prem_setup_gets_exact_review_with_prem_context(h):
    h.support.config.update(CONFIG);h.now=at('2026-10-27T08:04')
    row=candidate(h)
    assert h.support.queue_setup(row)['state']=='queued'
    assert h.support.process_one()
    packet=json.loads(h.calls[0]['input'][1]['content'])
    assert packet['hybrid_session']=='PREM' and packet['setup']['order_id']==row['order_id']
    session=packet['evidence']['DIRECTIONAL:'+row['order_id']+':session']
    assert session['value']['session']=='PREM'
    with h.support.connect() as db:assert db.execute('SELECT review_kind FROM reviews').fetchone()[0]=='SETUP'


def test_three_open_contexts_once_per_session_no_hourly_or_event_calls(h):
    h.support.config.update(CONFIG)
    for expected,time in enumerate(['05:01','09:31','13:31'],1):
        h.now=at('2026-10-27T'+time)
        assert h.support.process_one()
        assert len(h.calls)==expected
        h.now+=60
        assert not h.support.process_one() and len(h.calls)==expected
    h.now=at('2026-10-27T16:01')
    assert not h.support.process_one() and len(h.calls)==3
    with h.support.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reviews WHERE review_kind='CONTEXT'").fetchone()[0]==3


def test_failed_open_context_not_retried_within_session_or_restart(h):
    h.support.config.update(CONFIG);h.config.update(CONFIG);h.now=at('2026-10-27T05:01')
    attempts=[]
    def denied(payload):attempts.append(payload);raise ValueError('ai_http_401')
    h.support.transport=denied
    assert h.support.process_one() and len(attempts)==1
    h.now+=60
    assert not h.support.process_one() and len(attempts)==1
    fresh=ai.ContextSupport(h.config,reader=h.source,transport=denied,clock=lambda:h.now)
    assert not fresh.process_one() and len(attempts)==1
    with fresh.connect() as db:
        assert db.execute('SELECT error_code FROM reviews').fetchone()[0]=='ai_http_401'


def test_new_structure_event_does_not_spend_another_context_call(h,monkeypatch):
    h.support.config.update(CONFIG);h.now=at('2026-10-27T09:31')
    assert h.support.process_one()
    monkeypatch.setattr(life.context,'build',lambda p:dict(structure_events=dict(value=[dict(available_at=h.now+1,symbol='MNQ')])) )
    assert not life.context_due(h.support,{},h.support.session_at(h.now),'2026-10-27')


def test_prem_source_from_previous_session_not_sent_to_ai(h):
    h.support.config.update(CONFIG);h.now=at('2026-10-27T09:30')
    row=candidate(h);assert h.support.queue_setup(row)['state']=='queued'
    h.support.packet=lambda source,now:dict(as_of=at('2026-10-27T09:29'))
    assert not h.support.process_one() and not h.calls
    assert h.support.status=='WAITING_FOR_SESSION_DATA'

from test_directional_recovery_runtime import rig
from test_directional_recovery_wick import market

def test_recovery_uses_same_skip_session_filter(rig):
    f=rig;f.service.config['SKIP_SESSIONS']='NYAM'
    assert not f.run() and not f.calls and not f.posts


def test_recovery_response_crossing_session_end_cannot_dispatch(rig,monkeypatch):
    f=rig;end=f.now[0]/1000+0.5
    monkeypatch.setattr(ai,'session_at',lambda timestamp,config=None:dict(name='NYAM',ends_at=end) if timestamp<end else None)
    transport=f.service.transport
    def delayed(payload):
        response=transport(payload);f.now[0]+=1000;return response
    f.service.transport=delayed
    assert f.run() and len(f.calls)==1 and not f.posts
    payload=json.loads(f.calls[0]['input'][1]['content'])
    assert payload['review_expires_at']==end and payload['review_session']=='NYAM'
    with f.connect() as db:
        assert db.execute('SELECT error FROM nofill_review_results').fetchone()[0]=='RECOVERY_AI_RESPONSE_TOO_LATE'


def test_atomic_reservation_rechecks_opening_after_stale_due_check(h,monkeypatch):
    h.support.config.update(CONFIG);h.now=at('2026-10-27T09:31')
    assert h.support.process_one() and len(h.calls)==1
    h.now+=60
    monkeypatch.setattr(life,'context_due',lambda *a:True)
    assert not h.support.process_one() and len(h.calls)==1
    assert h.support.status=='WAITING_FOR_SETUP'
