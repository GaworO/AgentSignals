"""Synthetic calendar-only tests: no paid AI or broker access."""
import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import ict_news_context as news
import ict_shadow_research as research
from ict_shadow_cloud import Cloud


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()


def calendar(tmp_path, now, events=()):
    value=dict(received_at=now-60,week_start='2026-11-01',source='ForexFactory/JSON',
               event_count=5,raw_events=[dict(epoch=t,title=title,country=currency,impact='high',source='ForexFactory') for t,title,currency in events])
    (tmp_path/'news_calendar.json').write_text(json.dumps(value))
    return dict(DATA_DIR=str(tmp_path),NEWS_MAX_AGE_H='24'),value


def test_current_event_schedule_includes_week_and_no_invented_results(tmp_path):
    now=at('2026-11-04T09:35:00')
    config,_=calendar(tmp_path,now,[(at('2026-11-06T08:30:00'),'Non-Farm Employment Change','USD'),(at('2026-11-04T08:30:00'),'Core CPI m/m','USD')])
    value=news.build(config,now)
    assert value['status']=='CLEAR'
    assert [e['category'] for e in value['us_events_this_week']]==['CPI','NFP']
    assert len(value['us_events_today'])==1
    assert value['post_release_not_before']==at('2026-11-04T08:30:00')
    assert value['us_events_today'][0]['time_ny'].endswith('-05:00')
    assert not any(k in value['events'][0] for k in ('actual','forecast','previous'))


@pytest.mark.parametrize('delta,expected',[(-1801,'CLEAR'),(-1800,'BLACKOUT'),(0,'BLACKOUT'),(1800,'BLACKOUT'),(1801,'CLEAR')])
def test_existing_blackout_boundaries_preserved(tmp_path,delta,expected):
    release=at('2026-11-04T10:00:00');now=release+delta
    config,_=calendar(tmp_path,now,[(release,'ISM Services PMI','USD')])
    assert news.build(config,now)['status']==expected


@pytest.mark.parametrize('bad',['missing','bad_json','old','future','wrong_week','bad_max'])
def test_missing_stale_or_invalid_is_unknown(tmp_path,bad):
    now=at('2026-11-04T09:35:00');config,value=calendar(tmp_path,now)
    path=tmp_path/'news_calendar.json'
    if bad=='missing':path.unlink()
    elif bad=='bad_json':path.write_text('{')
    else:
        if bad=='old':value['received_at']=now-86401
        if bad=='future':value['received_at']=now+1
        if bad=='wrong_week':value['week_start']='2026-10-25'
        if bad=='bad_max':config['NEWS_MAX_AGE_H']='nan'
        path.write_text(json.dumps(value))
    assert news.build(config,now)['status']=='UNKNOWN'


def test_currency_filter_does_not_weaken_existing_global_blackout(tmp_path):
    now=at('2026-11-04T09:35:00')
    config,_=calendar(tmp_path,now,[(now,'BOE Governor Speaks','GBP')])
    result=news.build(config,now)
    assert result['status']=='BLACKOUT' and not result['us_events_today']


def test_unknown_or_blackout_uses_no_paid_review_slot(tmp_path):
    now=at('2026-11-04T09:35:00')
    config,_=calendar(tmp_path,now,[(now,'FOMC Statement','USD')])
    cloud=Cloud(config)
    assert cloud.select([],{'as_of':now},now)==(None,'ICT_NEWS_BLACKOUT',None)
    (tmp_path/'news_calendar.json').unlink()
    assert cloud.select([],{'as_of':now},now)==(None,'ICT_NEWS_UNKNOWN',None)
    with cloud.db() as db:assert db.execute('SELECT count(*) FROM costs').fetchone()[0]==0


def test_both_families_require_explicit_news_reasoning_in_new_schema():
    schema=research.schema('id',include_ifvg=True,include_news=True)
    for item in (schema,schema['properties']['ifvg_review']):
        assert 'news_and_session_risk' in item['properties']['assessments']['required']
    assert 'news_and_session_risk' not in research.schema('legacy')['properties']['assessments']['required']


def proposal():
    from test_ict_shadow_research import sample,T
    packet,decision=sample()
    packet['scheduled_news']=dict(status='CLEAR',observed_at=T,next_blackout_at=None,post_release_not_before=None)
    decision['assessments']['news_and_session_risk']=dict(reasoning='Synthetic calendar clear',evidence_ids=[],missing=[])
    return packet,decision,T


@pytest.mark.parametrize('status',['UNKNOWN','BLACKOUT'])
def test_actionable_proposals_cannot_ignore_news_status(status):
    packet,decision,T=proposal();packet['scheduled_news']['status']=status
    with pytest.raises(AssertionError,match='NEWS_NOT_CLEAR'):research.validate(packet,decision,T+10)
    decision.update(decision='WAIT',plan=None)
    assert research.validate(packet,decision,T+10)['state']=='WAIT'


def test_pre_release_fvg_cannot_be_recycled():
    packet,decision,T=proposal()
    packet['scheduled_news']['post_release_not_before']=T-120
    with pytest.raises(AssertionError,match='PRE_NEWS_SETUP'):research.validate(packet,decision,T+10)
    packet['scheduled_news']['post_release_not_before']=T-180
    assert research.validate(packet,decision,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'


def test_expiry_must_precede_blackout_even_after_ai_latency():
    packet,decision,T=proposal();packet['scheduled_news']['next_blackout_at']=T+600
    with pytest.raises(AssertionError,match='OVERLAPS_NEWS'):research.validate(packet,decision,T+10)
    decision['plan']['valid_until']=T+599
    assert research.validate(packet,decision,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'


def test_news_reasoning_required_and_future_context_rejected():
    packet,decision,T=proposal()
    del decision['assessments']['news_and_session_risk']
    with pytest.raises(AssertionError,match='ASSESSMENT_MISSING'):research.validate(packet,decision,T+10)
    packet,decision,T=proposal();packet['scheduled_news']['observed_at']=T+20
    with pytest.raises(AssertionError,match='NEWS_CONTEXT_STALE'):research.validate(packet,decision,T+10)


def test_ifvg_requires_new_inversion_but_may_use_prior_gap():
    from test_ict_ifvg import setup
    from test_ict_shadow_research import T
    packet,decision=setup(True)
    packet['scheduled_news']=dict(status='CLEAR',observed_at=T+60,next_blackout_at=None,post_release_not_before=T)
    decision['ifvg_review']['assessments']['news_and_session_risk']=dict(reasoning='New inversion after event',evidence_ids=[],missing=[])
    assert research.validate(packet,decision,T+70)['ifvg_validation']['state']=='VALIDATED_SHADOW_PROPOSAL'
    packet['scheduled_news']['post_release_not_before']=T+1
    assert research.validate(packet,decision,T+70)['ifvg_validation']['state']=='BLOCKED'
