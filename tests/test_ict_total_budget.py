from concurrent.futures import ThreadPoolExecutor
import pytest
from services.tanja.ict_agent import budget


def config(tmp_path):
    return dict(ICT_AI_TOTAL_BUDGET_DB=str(tmp_path/'shared.sqlite3'),ICT_AI_TOTAL_USD='8',ICT_AI_APPROVAL_ID='nyam-and-history')


def test_shared_cap_unknown_restart_and_immutable_approval(tmp_path):
    cfg=config(tmp_path)
    budget.reserve(cfg,'today',5)
    budget.finish(cfg,'today',None)
    budget.reserve(cfg,'history',3)
    with pytest.raises(ValueError,match='TOTAL_EXPERIMENT_BUDGET_CAP'):budget.reserve(dict(cfg),'restart',.01)
    with pytest.raises(ValueError,match='TOTAL_ALREADY_RESERVED'):budget.reserve(cfg,'today',1)
    with pytest.raises(ValueError,match='IMMUTABLE'):budget.reserve(dict(cfg,ICT_AI_APPROVAL_ID='replacement'),'reset',1)
    assert budget.status(cfg)['consumed_ceiling_usd']==8
    budget.finish(cfg,'history',.5)
    assert budget.status(cfg)['remaining_usd']==2.5


def test_concurrent_ledgers_cannot_overreserve(tmp_path):
    cfg=config(tmp_path)
    budget.reserve(cfg,'init',.01);budget.finish(cfg,'init',0)
    def attempt(i):
        try:budget.reserve(cfg,str(i),3);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=6) as pool:assert sum(pool.map(attempt,range(6)))==2
    assert budget.status(cfg)['consumed_ceiling_usd']==6


def test_usage_over_bound_halts_further_requests(tmp_path):
    cfg=config(tmp_path);budget.reserve(cfg,'call',1);budget.finish(cfg,'call',1.1)
    with pytest.raises(ValueError,match='RECONCILIATION'):budget.reserve(cfg,'more',1)
    assert budget.status(cfg)['status']=='HALTED'


def test_nyam_event_window_and_pacing():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services.tanja.ict_agent.runtime import review_gate
    now=datetime(2026,11,20,10,0,tzinfo=ZoneInfo('America/New_York')).timestamp()
    cfg={'ICT_AI_SESSION_DATE':'2026-11-20'}
    snap={'as_of':now-5,'events':[{'timeframe':5,'kind':'FVG'}]}
    assert review_gate(snap,cfg,now,0)=='READY'
    assert review_gate(snap,cfg,now,now-60)=='EVENT_COOLDOWN'
    assert review_gate(snap,cfg,now+120,0)=='STALE_OR_EMPTY'
    assert review_gate(snap,cfg,now+7200,0)=='OUTSIDE_NYAM'
    assert review_gate(snap,cfg,now+86400,0)=='OUTSIDE_AUTHORIZED_DATE'
