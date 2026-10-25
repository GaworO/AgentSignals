"""Synthetic account data only. No broker requests, orders or paid AI calls."""
import copy
import pytest
import directional_account_limits as limits
import directional_risk_revalidation as risk
from test_directional_account_sizing import case,cap
from test_directional_risk_revalidation import native


@pytest.fixture
def percent(monkeypatch):
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_MODE','EQUITY_PERCENT')
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT','0.5')
    monkeypatch.setenv('EXEC_MAX_QTY','40')


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('distance,expected',[(10,10),(20,5),(30,3),(60,1),(125,0)])
def test_structural_stop_equity_budget_costs_and_whole_contracts(case,percent,direction,distance,expected):
    args,proof,save=case
    order=args[2];sign=1 if direction=='LONG' else -1
    order.update(direction=direction,entry_price=1000.,stop_price=1000.-sign*distance,target_price=1000.+sign*2*distance)
    args=list(args);args[-1]=3.24
    save();original=copy.deepcopy(order)
    result=limits.capacity(*args,250)
    assert result['strategy_budget_usd']==243.60
    assert result['max_quantity']==expected
    assert result['allowed']==bool(expected)
    assert result['risk_per_contract_usd']==pytest.approx(distance*2+3.24)
    if expected:assert result['total_risk_usd']<=48721.05*.005
    assert order==original and result['gross_2r_target']==order['target_price']


def test_budget_uses_equity_not_start_balance_and_never_rounds_up(percent):
    snap=lambda equity:dict(account=dict(balance=dict(netLiq=equity)))
    assert limits.trade_budget(snap(48721.05),250)==243.60
    assert limits.trade_budget(snap(51000),250)==255
    assert limits.trade_budget(snap(47000),250)==235


@pytest.mark.parametrize('pct',['', '0','-0.5','0.51','NaN','Infinity'])
def test_invalid_percentage_blocks_without_fixed_fallback(percent,monkeypatch,pct):
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT',pct)
    with pytest.raises(Exception):limits.trade_budget(dict(account=dict(balance=dict(netLiq=48721.05))),250)


@pytest.mark.parametrize('equity',[None,True,float('nan'),float('inf'),0,-1,'48721.05'])
def test_unknown_equity_blocks(percent,equity):
    with pytest.raises((ValueError,TypeError)):limits.trade_budget(dict(account=dict(balance=dict(netLiq=equity))),250)


def test_margin_one_loss_and_daily_capacity_still_apply(case,percent):
    args,proof,save=case
    proof['account_controls']['margin_max_new_micro_contracts']=2
    assert cap(case)['max_quantity']==2
    proof['account_controls']['losses_today']=1
    assert cap(case)['reason']=='account_wide_one_loss_stop'


def test_pending_account_change_recalculates_budget_at_final_gate(native,percent,monkeypatch):
    f=native
    # Force the freshly recomputed budget below the prepared risk to isolate
    # the post-poll budget barrier without forging broker reconciliation.
    monkeypatch.setattr(limits,'trade_budget',lambda snapshot,legacy:1.)
    result=f.validator(dict(f.order))
    assert not result['allowed'] and result['reason']=='cost_inclusive_trade_risk_budget'
    assert f.posts==[]


def test_larger_candle_quantity_requires_explicit_policy_cap_and_preserves_ai_binding():
    from test_directional_resting_policy import case as market_case
    import execution_validation as gate
    a=market_case();a[5]['max_quantity']=40;a[0]['quantity']=3;a[4]['setup']['quantity']=3
    assert gate.evaluate(*a) is None
    a[0]['quantity']=41
    assert gate.evaluate(*a)=='resting_limit_quantity_cap'
