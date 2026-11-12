from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from ict_sessions import session,review_pacing
from test_ict_shadow_cloud import c,payload


def at(value,zone='America/New_York'):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp()

@pytest.mark.parametrize('start,interval',[
    ('2026-11-12T03:00:00',900),
    ('2026-11-12T09:30:00',675),
    ('2026-11-12T13:30:00',1125),
])
def test_continuous_events_cannot_burn_eight_calls_early(c,start,interval):
    start=at(start);end=session(start)[1];rows=[];sent=[]
    # A fresh event on every worker tick is the busiest possible input.
    for now in range(int(start+60),int(end),30):
        packet=dict(as_of=now,trigger_events=[dict(available_at=now-1,kind='MSS')])
        slot,reason,event=c.select(rows,packet,now)
        if slot is not None:
            rid='review_'+str(slot)
            c.reserve(rid,payload(),now)
            rows.append(dict(id=rid,status='research_completed',requested_at=now))
            sent.append(now)
    assert len(sent)==8
    assert all(b-a>=interval for a,b in zip(sent,sent[1:]))
    assert sent[-1]>=start+7*interval
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
    assert state['ready'] and state['interval_seconds']==1125


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
    assert out['state']=='OBSERVING' and out['next_at']==last+675
