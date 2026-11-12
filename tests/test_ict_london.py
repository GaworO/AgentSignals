"""Offline schedule, accounting, news and UI contract checks. No live calls."""
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
import ict_sessions as schedule
import ict_ai_call_status as status
import ict_history
import ict_news_context as news
from ict_shadow_cloud import Cloud, DAILY_USD
from test_ict_shadow_cloud import c, payload, ts
from test_ict_ai_call_status import snapshot


def london(value):
    return datetime.fromisoformat(value).replace(tzinfo=schedule.LONDON).timestamp()


@pytest.mark.parametrize('day,allowed',[(9,False),(10,True),(11,True),(12,True),(13,True),(14,False),(15,False),(16,False)])
def test_tuesday_to_friday_only(day,allowed):
    stamp=london(f'2026-11-{day:02}T08:00:00')
    window=schedule.session(stamp)
    assert bool(window)==allowed
    assert schedule.daily_call_limit(f'2026-11-{day:02}')==((72 if allowed else 48) if day>=12 else (24 if allowed else 16))
    if allowed:
        assert window[0]=='LONDON' and window[1]-window[2]==7200
        assert schedule.session(stamp-1) is None
        assert schedule.session(window[1]-1)[0]=='LONDON'
        assert schedule.session(window[1]) is None


@pytest.mark.parametrize('day,ny_hour',[('2026-11-10',3),('2027-03-16',4),('2027-03-30',3),('2027-11-02',4),('2027-11-09',3)])
def test_uk_clock_not_fixed_ny_offset(day,ny_hour):
    stamp=london(day+'T08:00:00')
    assert datetime.fromtimestamp(stamp,schedule.NY).hour==ny_hour
    assert schedule.session(stamp)[0]=='LONDON'


def test_historical_caps_and_monday_ny_unchanged():
    assert schedule.daily_call_limit('2026-10-30')==12
    assert schedule.daily_call_limit('2026-11-06')==16
    assert schedule.session(london('2026-11-06T08:10:00')) is None
    assert schedule.session(ts('2026-11-09T09:30:00'))[0]=='NYAM'
    assert schedule.session(ts('2026-11-09T13:30:00'))[0]=='NYPM'
    assert DAILY_USD==20


def test_eight_london_reservations_survive_restart_and_ny_keeps_eight(c):
    day='2026-11-10';opening=london(day+'T08:01:00')
    for i in range(8):c.reserve('ICT_'+str(i),payload(),opening+i*120)
    restarted=Cloud(c.config)
    now=london(day+'T09:00:00')
    assert restarted.select([],dict(as_of=now,trigger_events=[]),now)[1]=='SESSION_CALL_CAP'
    with pytest.raises(ValueError,match='SESSION_CALL_CAP'):restarted.reserve('ICT_8',payload(),now)
    am=ts(day+'T09:31:00')
    assert restarted.select([],dict(as_of=am,trigger_events=[]),am)[0]==8
    for i in range(8):restarted.reserve('ICT_'+str(i+8),payload(),am+i*120)
    with pytest.raises(ValueError,match='SESSION_CALL_CAP'):restarted.reserve('ICT_16',payload(),am+1200)
    pm=ts(day+'T13:31:00')
    for i in range(8):restarted.reserve('ICT_'+str(i+16),payload(),pm+i*120)
    with pytest.raises(ValueError,match='DAILY_CALL_CAP'):restarted.reserve('ICT_24',payload(),pm+1200)
    with restarted.db() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM costs')]
    assert len(rows)==24 and all(r['amount'] is None for r in rows)
    assert [(b['session'],b['used'],b['cap']) for b in schedule.session_budgets(day,rows)]==[('LONDON',8,8),('NYAM',8,8),('NYPM',8,8)]


def test_unused_london_calls_cannot_be_spent_on_ny(c):
    at=ts('2026-11-10T09:31:00')
    for i in range(8):c.reserve('ICT_'+str(i),payload(),at+i*120)
    with pytest.raises(ValueError,match='SESSION_CALL_CAP'):c.reserve('ICT_8',payload(),at+1200)


def test_concurrent_reservations_never_exceed_eight(c):
    at=london('2026-11-10T08:10:00')
    def attempt(i):
        try:c.reserve('ICT_'+str(i),payload(),at);return True
        except ValueError as e:
            assert str(e)=='SESSION_CALL_CAP';return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt,range(12)))==8


@pytest.mark.parametrize('state',['BLACKOUT','POLICY_BLOCKED','UNKNOWN'])
def test_new_session_does_not_bypass_calendar(c,monkeypatch,state):
    at=london('2026-11-10T08:10:00')
    monkeypatch.setattr(news,'build',lambda *a:dict(status=state))
    assert c.select([],dict(as_of=at,trigger_events=[]),at)[1]=='ICT_NEWS_'+state
    with c.db() as db:assert db.execute('SELECT COUNT(*) FROM costs').fetchone()[0]==0


def test_fomc_and_nfp_blocks_cover_london():
    fomc=[dict(id='fomc',at=ts('2026-11-12T14:00:00'),category='FOMC')]
    nfp=[dict(id='nfp',at=ts('2026-12-04T08:30:00'),category='NFP')]
    for events,at in [(fomc,london('2026-11-12T08:10:00')),(nfp,london('2026-12-03T08:10:00')),(nfp,london('2026-12-04T08:10:00'))]:
        for profile in news.PROFILES:
            assert any(w['start']<=at<w['end'] for w in news.conservative_windows(events,profile))


def test_next_session_and_used_london_budget_are_displayed():
    assert status.next_window(ts('2026-11-09T16:00:00'))==london('2026-11-10T08:00:00')
    assert status.next_window(ts('2026-11-13T16:00:00'))==ts('2026-11-16T09:30:00')
    at=london('2026-11-10T09:00:00');s=snapshot(at)
    s.update(daily_call_cap=24,calls_today=8,session_budgets=[dict(session='LONDON',cap=8,used=8)])
    value=status.describe(s,dict(status='CLEAR'),True)
    assert value['state']=='PAUSED' and 'all 8' in value['reason']
    assert value['next_at']==ts('2026-11-10T09:30:00')


def test_history_has_london_filter_without_relabeling_old_calls():
    at=london('2026-11-10T08:10:00')
    assert ict_history.session(at)=='LONDON'
    assert ict_history.session(london('2026-11-09T08:10:00'))=='OTHER'
    history=ict_history.build(dict(now=at+60,reviews=[]),[dict(id='one',at=at,amount=None,reserve=.2)],[],[],'day','2026-11-10','LONDON')
    assert history['summary']['calls']==1 and history['summary']['reserved_usd']==.2
