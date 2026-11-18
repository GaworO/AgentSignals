"""Operator-approved session extension. All AI/broker fixtures are offline."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import ict_sessions as schedule
import ict_execution as execution
import ict_shadow_research as research
from ict_shadow_cloud import Cloud, DAILY_USD
from test_ict_shadow_cloud import c, payload
from test_ict_execution import routed, record, save, T


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=schedule.NY).timestamp()


@pytest.mark.parametrize('date', ['2026-11-18', '2026-11-23', '2027-03-15', '2027-11-08'])
def test_nyam_boundary_uses_new_york_time_in_both_dst_regimes(date):
    end = at(date+'T11:15:00')
    assert schedule.session(end-1) == ('NYAM', end, at(date+'T09:30:00'))
    assert schedule.session(at(date+'T11:00:00'))[0] == 'NYAM'
    assert schedule.session(end) is None
    assert schedule.session(at(date+'T13:29:59')) is None
    assert schedule.session(at(date+'T13:30:00'))[0] == 'NYPM'


def test_history_weekends_london_pm_and_dollar_budget_preserved():
    assert schedule.session(at('2026-11-17T11:00:00')) is None
    assert schedule.session_call_limit('NYAM', '2026-11-17') == 18
    assert schedule.daily_call_limit('2026-11-17') == 72
    assert schedule.daily_call_limit('2026-10-30') == 12
    assert schedule.session(at('2026-11-21T11:05:00')) is None
    assert schedule.session(at('2026-11-22T11:05:00')) is None
    assert schedule.daily_call_limit('2026-11-18') == 75
    assert schedule.daily_call_limit('2026-11-23') == 51
    assert schedule.session_call_limit('LONDON', '2026-11-18') == 24
    assert schedule.session_call_limit('NYPM', '2026-11-18') == 30
    assert DAILY_USD == 20


def test_five_minute_cadence_adds_only_three_slots_and_survives_restart(c):
    start = at('2026-11-18T09:31:00')
    rows = []
    for i in range(21):
        now = start+i*300
        packet = dict(as_of=now, trigger_events=[])
        scanner = Cloud(c.config) if i >= 18 else c
        assert scanner.select(rows, packet, now)[0] == i
        scanner.reserve('review_'+str(i), payload(), now)
        rows.append(dict(id='review_'+str(i), status='research_completed', requested_at=now))
    assert [datetime.fromtimestamp(r['requested_at'], schedule.NY).strftime('%H:%M') for r in rows[-3:]] == ['11:01', '11:06', '11:11']
    with pytest.raises(ValueError, match='SESSION_CALL_CAP'):
        scanner.reserve('extra_21', payload(), at('2026-11-18T11:14:59'))
    assert scanner.select(rows, packet, at('2026-11-18T11:15:00'))[1] == 'OUTSIDE_SESSION'
    with scanner.db() as db:
        assert db.execute('SELECT COUNT(*) FROM costs').fetchone()[0] == 21


def test_extra_window_keeps_unknown_attempt_and_dollar_guards(c):
    now = at('2026-11-18T11:05:00')
    rows = [dict(id='unknown_0', status='research_unknown', requested_at=now-600)]
    assert c.select(rows, dict(as_of=now, trigger_events=[]), now)[1] == 'UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION'
    with c.db() as db:
        db.execute('INSERT INTO costs VALUES(?,?,?,?,NULL,?)', ('burned_0','2026-11-18',now-600,19.99,'UNKNOWN_RESERVED'))
    with pytest.raises(ValueError, match='DAILY_DOLLAR_CAP'):
        c.reserve('extra_1', payload(), now)


def shifted_record(short, cutoff, expiry):
    r = record(short)
    delta = cutoff-T
    p, d = r['packet'], r['decision']
    p['as_of'] += delta
    p['session_end'] = schedule.session(cutoff)[1]
    for e in p['evidence'].values():
        for key in ('time', 'available_at'):
            if key in e:
                e[key] += delta
    d['plan']['valid_until'] = expiry
    p.pop('packet_id')
    p['packet_id'] = research.digest(p)
    d['packet_id'] = p['packet_id']
    r.update(id='ICT_INDEPENDENT_NYAM_20261118_99', requested_at=cutoff+1, received_at=cutoff+10)
    r['response']['output'][0]['content'][0]['text'] = json.dumps(d)
    r['validation'] = research.validate(p, d, r['received_at'])
    return r


@pytest.mark.parametrize('short', [False, True])
def test_fresh_proposal_can_use_extra_window_but_not_rewrite_expiry(routed, monkeypatch, short):
    import guardrails
    cutoff = at('2026-11-18T11:05:00')
    end = at('2026-11-18T11:15:00')
    r = shifted_record(short, cutoff, end)
    root = routed.tmp.name
    monkeypatch.setenv('DATA_DIR', root)
    path = __import__('pathlib').Path(root)/'research/ict_2022_nyam_20261118'/ (r['id']+'.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(r))
    order = execution.from_record(r, cutoff+11)
    assert order['expiry_ms'] == end*1000
    assert execution.permission(order, clock=lambda:cutoff+11) is None
    # Guard receives explicit ICT NYAM, not the unrelated global NYL label.
    assert guardrails._sess_of(execution.signal(order)) == 'NYAM'
    assert execution.permission(order, clock=lambda:end) == 'ict_setup_expired'
    changed = dict(order, expiry_ms=int((end+60)*1000))
    assert execution.permission(changed, clock=lambda:end) == 'ict_outside_session'
    monkeypatch.setenv('EXEC_KILL_SWITCH', 'true')
    assert execution.permission(order, clock=lambda:cutoff+11)
    assert not routed.posts
    with pytest.raises((ValueError, AssertionError)):
        execution.from_record(r, end)
    bad = json.loads(json.dumps(r))
    bad['decision']['plan']['valid_until'] = end+60
    with pytest.raises(AssertionError, match='EXPIRED_OR_INVALID_VALIDITY'):
        research.validate(bad['packet'], bad['decision'], cutoff+10)


def test_old_1100_expiry_cannot_be_revived_and_late_ai_still_fails(monkeypatch):
    monkeypatch.setenv('AB_V3_CONTRACT', 'MNQZ2026')
    old_end = at('2026-11-18T11:00:00')
    r = shifted_record(False, old_end-120, old_end)
    with pytest.raises((ValueError, AssertionError)):
        execution.from_record(r, old_end+1)
    # Even a newly supplied 11:15 session cannot extend an exact old approval.
    with pytest.raises(AssertionError, match='EXPIRED_OR_INVALID_VALIDITY'):
        research.validate(r['packet'], r['decision'], old_end)


def test_prepare_supplies_1115_to_prompt_and_pending_review(c, monkeypatch):
    import ict_pending_review
    monkeypatch.setattr(execution, 'ifvg_permission', lambda **kw: 'disabled')
    monkeypatch.setattr(ict_pending_review, 'snapshot', lambda now: None)
    now = at('2026-11-18T11:01:00')
    p = c.prepare(dict(as_of=now, evidence={}), now)
    assert p['session_name'] == 'NYAM'
    assert p['session_end'] == at('2026-11-18T11:15:00')
    with pytest.raises(ValueError, match='SESSION_ENDED'):
        c.prepare(dict(as_of=now, evidence={}), p['session_end'])
