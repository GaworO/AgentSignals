"""Read-only reporting: no paid transport, broker writes, or invented P&L."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from ict_shadow_cloud import Cloud, dashboard_analytics


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()


def review(name, state, pnl=None, valid=True, at='2026-10-30T10:00:00'):
    return {'id':name,'requested_at':ts(at),'decision':{'plan':{'direction':'LONG'}},
            'validation':{'state':'VALIDATED_SHADOW_PROPOSAL' if valid else 'INVALID_OUTPUT'},
            'outcome':{'state':state,'gross_usd_per_mnq':pnl}}


def test_pf_uses_only_validated_closed_simulations():
    rows=[review('win','SIMULATED_TARGET',60),review('loss','SIMULATED_STOP',-20),
          review('no','SIMULATED_NO_FILL',900),review('bad','SIMULATED_TARGET',1000,False),
          review('gap','UNKNOWN_CANDLE_GAP'),review('open','SIMULATED_OPEN'),
          review('wait','SIMULATED_WAITING'),review('inv','SIMULATED_INVALIDATED_BEFORE_FILL')]
    a=dashboard_analytics([],rows,ts('2026-10-30T15:00:00'))['periods']['today']
    assert a['gross_pnl_per_mnq']==40 and a['profit_factor']==3
    assert (a['wins'],a['losses'],a['no_fill'],a['rejected'])==(1,1,1,1)
    assert (a['open'],a['pending'],a['invalidated'],a['uncertain'])==(1,1,1,1)
    assert a['win_rate']==.5


@pytest.mark.parametrize('rows,expected',[
    ([], 'NO_CLOSED_TRADES'),([review('w','SIMULATED_TARGET',60)],'NO_LOSSES'),
    ([review('w','SIMULATED_TARGET')],'INCOMPLETE_PNL')])
def test_undefined_pf_not_infinity_or_zero(rows,expected):
    a=dashboard_analytics([],rows,ts('2026-10-30T15:00:00'))['periods']['today']
    assert a['profit_factor'] is None and a['pf_state']==expected
    if expected!='NO_LOSSES':assert a['gross_pnl_per_mnq'] is None


def test_ny_calendar_spend_and_reserves_across_month_week_dst():
    # Monday after US DST: previous UTC Monday still belongs to NY Sunday.
    rows=[{'day':d,'amount':amount,'reserve':reserve} for d,amount,reserve in [
        ('2026-10-30',2,5),('2026-11-01',3,5),('2026-11-02',None,.75),('2026-11-03',99,0)]]
    p=dashboard_analytics(rows,[],ts('2026-11-02T00:30:00'))['periods']
    assert p['today']['estimated_usd']==0 and p['today']['reserved_usd']==.75
    assert p['week']['from']=='2026-11-02' and p['week']['calls']==1
    assert p['month']['estimated_usd']==3 and p['month']['calls']==2
    assert p['all']['estimated_usd']==5 and p['all']['reserved_usd']==.75
    assert p['all']['unknown_cost_attempts']==1


def test_trade_period_is_creation_date_not_result_or_utc():
    r=review('x','SIMULATED_TARGET',60,at='2026-10-29T23:58:00')
    p=dashboard_analytics([], [r],ts('2026-10-30T09:00:00'))
    assert p['periods']['today']['proposals']==0
    assert p['periods']['week']['wins']==1
    assert p['period_basis']=='PROPOSAL_DATE_AMERICA_NEW_YORK'


def test_history_not_truncated_to_latest_100(tmp_path):
    c=Cloud({'DATA_DIR':str(tmp_path)})
    folder=c.root/'ict_2022_nyam_20261030';folder.mkdir()
    for i in range(105):
        r={'id':f'ICT_INDEPENDENT_NYAM_20261030_{i}','requested_at':ts('2026-10-30T10:00:00')+i,
           'decision':{'decision':'WAIT','plan':None},'validation':{'state':'WAIT'}}
        (folder/(r['id']+'.json')).write_text(json.dumps(r))
    assert len(c.records())==105
    assert len(c.state()['reviews'])==105


def test_state_aggregates_saved_costs_without_changing_ledger(tmp_path,monkeypatch):
    import ict_shadow_cloud as cloud
    c=Cloud({'DATA_DIR':str(tmp_path)});monkeypatch.setattr(cloud.time,'time',lambda:ts('2026-10-30T11:00:00'))
    with c.db() as db:
        db.executemany('INSERT INTO costs VALUES(?,?,?,?,?,?)',[
            ('old','2026-10-27',ts('2026-10-27T10:00:00'),1,.3,'ESTIMATED'),
            ('new','2026-10-30',ts('2026-10-30T10:00:00'),1,None,'UNKNOWN_RESERVED')])
    s=c.state()
    assert s['calls_today']==1 and s['estimated_or_reserved_usd']==1
    assert s['analytics']['periods']['week']['estimated_usd']==.3
    assert s['analytics']['periods']['week']['reserved_usd']==1
    with c.db() as db:assert db.execute('SELECT COUNT(*) FROM costs').fetchone()[0]==2
    assert s['execution_authorized'] is False


def test_review_snapshot_time_is_recorded_not_inferred(tmp_path):
    c=Cloud({'DATA_DIR':str(tmp_path)})
    folder=c.root/'ict_2022_nyam_20261030';folder.mkdir()
    records=[{'id':'ICT_INDEPENDENT_NYAM_20261030_0','requested_at':ts('2026-10-30T10:00:00'),
              'packet':{'as_of':ts('2026-10-30T09:59:00')},'decision':{'decision':'WAIT','summary':'No displacement.'}},
             {'id':'ICT_INDEPENDENT_NYAM_20261030_1','requested_at':ts('2026-10-30T10:20:00'),
              'decision':{'decision':'WAIT','summary':'No snapshot saved.'}}]
    for r in records:(folder/(r['id']+'.json')).write_text(json.dumps(r))
    result={r['id']:r for r in c.state()['reviews']}
    assert result[records[0]['id']]['market_as_of']==ts('2026-10-30T09:59:00')
    assert result[records[1]['id']]['market_as_of'] is None
    assert result[records[0]['id']]['decision']==records[0]['decision']
