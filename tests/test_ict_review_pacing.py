from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from ict_sessions import session,review_pacing
from test_ict_shadow_cloud import c,payload


def at(value,zone='America/New_York'):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp()

@pytest.mark.parametrize('start,interval',[
    ('2026-11-12T03:00:00',300),
    ('2026-11-12T09:30:00',300),
    ('2026-11-12T13:30:00',300),
])
@pytest.mark.parametrize('events',[False,True])
def test_five_minute_reviews_cover_whole_session(c,start,interval,events):
    start=at(start);end=session(start)[1];rows=[];sent=[]
    # A fresh event on every worker tick is the busiest possible input.
    for now in range(int(start+60),int(end),30):
        packet=dict(as_of=now,trigger_events=[dict(available_at=now-1,kind='MSS')] if events else [])
        slot,reason,event=c.select(rows,packet,now)
        if slot is not None:
            rid='review_'+str(slot)
            c.reserve(rid,payload(),now)
            rows.append(dict(id=rid,status='research_completed',requested_at=now))
            sent.append(now)
    assert len(sent)==int((end-start)/300)
    assert all(b-a>=interval for a,b in zip(sent,sent[1:]))
    assert sent[-1]>=end-300
    assert c.select(rows,dict(as_of=end,trigger_events=[]),end)[1]=='OUTSIDE_SESSION'


def test_restart_burned_attempts_and_no_catchup_burst(c):
    from ict_shadow_cloud import Cloud
    now=at('2026-11-12T09:31:00')
    c.reserve('review_0',payload(),now)  # No response row: attempt remains charged.
    restarted=Cloud(c.config)
    fresh=dict(as_of=now+120,trigger_events=[dict(available_at=now+119,kind='MSS')])
    assert restarted.select([],fresh,now+120)[1]=='SESSION_REVIEW_PACING'
    # After a long pause, a call is possible, but unused time cannot buy a burst.
    later=now+1800
    assert restarted.select([],dict(as_of=later,trigger_events=[]),later)[0]==1
    restarted.reserve('review_1',payload(),later)
    assert restarted.select([],dict(as_of=later+120,trigger_events=[]),later+120)[1]=='SESSION_REVIEW_PACING'


def test_prior_session_does_not_use_next_sessions_pacing():
    now=at('2026-11-12T13:31:00')
    state=review_pacing(now,[dict(at=at('2026-11-12T10:55:00'))])
    assert state['ready'] and state['interval_seconds']==300


def test_no_review_before_first_closed_candle():
    now=at('2026-11-12T09:30:30')
    assert not review_pacing(now,[])['ready']


def test_dashboard_explains_next_possible_review(c,monkeypatch):
    import ict_ai_call_status
    now=at('2026-11-12T09:35:00');last=now-120
    state=dict(now=now,calls_today=1,daily_call_cap=24,estimated_or_reserved_usd=.2,
        daily_usd_cap=10,worker=dict(at=now),last_check=dict(at=now,state='SESSION_REVIEW_PACING'),
        review_pacing=review_pacing(now,[dict(at=last)]))
    out=ict_ai_call_status.describe(state,dict(status='CLEAR'),True)
    assert out['state']=='OBSERVING' and out['next_at']==last+300


def test_atomic_concurrent_requests_cannot_exceed_cadence(c):
    from concurrent.futures import ThreadPoolExecutor
    now=at('2026-11-12T09:31:00')
    def attempt(i):
        try:c.reserve('race_'+str(i),payload(),now);return True
        except ValueError as e:
            assert str(e)=='SESSION_REVIEW_PACING';return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt,range(6)))==1
    with c.db() as db:assert db.execute('SELECT count(*) FROM costs').fetchone()[0]==1


def test_twenty_dollar_limit_unknown_charges_and_production_separation(c):
    from ict_shadow_cloud import DAILY_USD
    from ict_sessions import daily_call_limit,session_call_limit
    now=at('2026-11-12T09:31:00')
    assert DAILY_USD==20 and daily_call_limit('2026-11-12')==72
    assert daily_call_limit('2026-11-16')==48
    assert [session_call_limit(s,'2026-11-12') for s in ('LONDON','NYAM','NYPM')]==[24,18,30]
    with c.db() as db:
        db.execute('INSERT INTO costs VALUES(?,?,?,?,NULL,?)',('burned_0','2026-11-12',now-600,19.99,'UNKNOWN_RESERVED'))
    with pytest.raises(ValueError,match='DAILY_DOLLAR_CAP'):c.reserve('new_1',payload(),now)
    assert not (c.root.parent/'ab_ai_context.sqlite3').exists()


def test_unknown_request_remains_blocked_at_next_due_time(c):
    now=at('2026-11-12T09:41:00')
    rows=[dict(id='unknown_0',status='research_unknown',requested_at=now-600)]
    assert c.select(rows,dict(as_of=now,trigger_events=[]),now)[1]=='UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION'


def test_failed_response_keeps_five_minute_spacing(c):
    now=at('2026-11-12T09:31:00');c.reserve('failed_0',payload(),now);c.finish('failed_0',None,'timeout')
    with pytest.raises(ValueError,match='SESSION_REVIEW_PACING'):c.reserve('early_1',payload(),now+299)
    c.reserve('due_1',payload(),now+300)


@pytest.mark.parametrize('news',['UNKNOWN','BLACKOUT','POLICY_BLOCKED'])
def test_due_review_never_overrides_news(c,monkeypatch,news):
    import ict_news_context
    now=at('2026-11-12T09:31:00')
    monkeypatch.setattr(ict_news_context,'build',lambda *a:dict(status=news))
    assert c.select([],dict(as_of=now,trigger_events=[]),now)[1]=='ICT_NEWS_'+news
