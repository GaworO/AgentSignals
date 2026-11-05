"""Display-only schedule tests; no AI, broker or network requests."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import ict_ai_call_status as status
import ict_news_context
from test_ict_news_context import calendar, at


def snapshot(now):
    return dict(now=now, worker=dict(at=now), market=dict(as_of=now-60),
                calls_today=0, daily_call_cap=16, estimated_or_reserved_usd=0,
                daily_usd_cap=8, last_check={})


def test_nfp_pause_shows_monday_not_saturday_or_thursday_afternoon(tmp_path):
    now=at('2026-11-05T12:00:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-06T08:30:00'),'Non-Farm Employment Change','USD')])
    result=status.describe(snapshot(now),ict_news_context.build(config,now),True)
    assert result['state']=='PAUSED' and 'NFP week' in result['reason']
    assert result['next_at']==at('2026-11-09T09:30:00')
    assert datetime.fromtimestamp(result['next_at'],ZoneInfo('Europe/Berlin')).hour==15


def test_nfp_boundary_and_upcoming_pause(tmp_path):
    now=at('2026-11-04T10:59:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-06T08:30:00'),'Non-Farm Employment Change','USD')])
    result=status.describe(snapshot(now),ict_news_context.build(config,now),True)
    assert result['state']=='OBSERVING'
    assert result['upcoming_at']==at('2026-11-04T11:00:00')
    result=status.describe(snapshot(now+60),ict_news_context.build(config,now+60),True)
    assert result['state']=='PAUSED'


def test_fomc_morning_merges_statement_and_conference_buffers(tmp_path):
    now=at('2026-11-05T09:45:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-05T14:00:00'),'FOMC Statement','USD'),
                                   (at('2026-11-05T14:30:00'),'FOMC Press Conference','USD')])
    result=status.describe(snapshot(now),ict_news_context.build(config,now),True)
    assert 'FOMC' in result['reason']
    assert result['next_at']==at('2026-11-05T15:00:01')


def test_non_us_blackout_names_event_and_inclusive_end(tmp_path):
    now=at('2026-11-05T10:30:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-05T10:00:00'),'BOE Governor Speaks','GBP')])
    result=status.describe(snapshot(now),ict_news_context.build(config,now),True)
    assert result['state']=='PAUSED' and 'BOE' in result['reason']
    assert result['next_at']==now+1


@pytest.mark.parametrize('when,expected',[
    ('2026-11-05T08:00:00','2026-11-05T09:30:00'),
    ('2026-11-05T11:00:00','2026-11-05T13:30:00'),
    ('2026-11-06T16:00:00','2026-11-09T09:30:00'),
    ('2026-11-07T12:00:00','2026-11-09T09:30:00'),
])
def test_session_boundaries(when,expected):
    result=status.describe(snapshot(at(when)),dict(status='CLEAR'),True)
    assert result['state']=='PAUSED' and result['next_at']==at(expected)


def test_calendar_unknown_never_promises_restart(tmp_path):
    now=at('2026-11-05T10:00:00')
    news=ict_news_context.build(dict(DATA_DIR=str(tmp_path)),now)
    result=status.describe(snapshot(now),news,True)
    assert result['state']=='CHECK_REQUIRED' and result['next_at'] is None


@pytest.mark.parametrize('key,value',[('calls_today',16),('estimated_or_reserved_usd',8)])
def test_daily_limit_does_not_claim_afternoon_reset(key,value):
    state=snapshot(at('2026-11-05T10:00:00'));state[key]=value
    result=status.describe(state,dict(status='CLEAR'),True)
    assert result['state']=='PAUSED' and result['next_at']==at('2026-11-06T09:30:00')


@pytest.mark.parametrize('reason',['THREE_REVIEWS_PER_30_MINUTES','90_SECOND_COOLDOWN',
    'DEFER_PRODUCTION_AI_UNRESOLVED','UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION','WATCHING_CANDLES_NO_NEW_EVENT'])
def test_real_waits_are_shown_without_false_resume_promises(reason):
    state=snapshot(at('2026-11-05T10:00:00'))
    state['last_check']=dict(at=state['now'],state=reason)
    result=status.describe(state,dict(status='CLEAR'),True)
    assert result['state']=='WAITING' and result['next_at'] is None


def test_stale_heartbeat_and_disabled_scanner_are_not_ready():
    state=snapshot(at('2026-11-05T10:00:00'));state['worker']['at']-=181
    assert status.describe(state,dict(status='CLEAR'),True)['state']=='CHECK_REQUIRED'
    assert 'disabled' in status.describe(state,dict(status='CLEAR'),False)['reason']


def test_old_wait_does_not_override_current_calendar_pause(tmp_path):
    now=at('2026-11-05T10:00:00');state=snapshot(now)
    state['last_check']=dict(at=now-86400,state='WATCHING_CANDLES_NO_NEW_EVENT')
    config,_=calendar(tmp_path,now,[(at('2026-11-06T08:30:00'),'Non-Farm Employment Change','USD')])
    assert 'NFP week' in status.describe(state,ict_news_context.build(config,now),True)['reason']


def test_dashboard_recomputes_calendar_without_worker_or_ledger_writes(tmp_path,monkeypatch):
    import ict_shadow_cloud
    now=at('2026-11-05T12:00:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-06T08:30:00'),'Non-Farm Employment Change','USD')])
    config['ICT_SHADOW_ENABLED']='true'
    cloud=ict_shadow_cloud.Cloud(config)
    cloud.put('scheduled_news',dict(status='CLEAR',observed_at=now-86400))
    with cloud.db() as db:
        before=[tuple(r) for r in db.execute('SELECT * FROM state')]
        costs=[tuple(r) for r in db.execute('SELECT * FROM costs')]
    monkeypatch.setattr(ict_shadow_cloud.time,'time',lambda:now)
    monkeypatch.setattr(cloud,'tick',lambda:pytest.fail('Display cannot start a worker check'))
    value=cloud.state()
    assert value['scheduled_news']['status']=='POLICY_BLOCKED'
    assert value['ai_call_status']['state']=='PAUSED'
    with cloud.db() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM state')]==before
        assert [tuple(r) for r in db.execute('SELECT * FROM costs')]==costs
