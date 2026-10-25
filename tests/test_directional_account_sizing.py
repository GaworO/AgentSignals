"""Synthetic account controls; no API, AI calls or broker orders."""
import copy
import importlib
import json

import pytest
import directional_account_limits as limits
import directional_risk_revalidation as risk
from test_directional_completion import allowance_fixture
from test_directional_risk_revalidation import native


@pytest.fixture
def case(tmp_path,monkeypatch):
    args,proof,path=allowance_fixture(tmp_path,monkeypatch)
    monkeypatch.setenv('EXEC_MAX_QTY','15')
    monkeypatch.setenv('DD_BUFFER','100')
    monkeypatch.setenv('SETUP_GROUP_FLOOR_RESERVE_USD','100')
    monkeypatch.delenv('DD_FLOOR',raising=False)
    def save():path.write_text(json.dumps(proof))
    return args,proof,save


def cap(case,budget=250):
    args,proof,save=case;save()
    return limits.capacity(*args,budget)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('budget,qty',[(21.9,1),(250,11),(21.89,0)])
def test_cost_inclusive_long_short_sizing_preserves_geometry(case,direction,budget,qty):
    args,proof,save=case
    args[2].update(direction=direction,stop_price=90 if direction=='LONG' else 110)
    original=copy.deepcopy(args[2]);r=cap(case,budget)
    assert r['max_quantity']==qty and r['allowed']==bool(qty)
    assert r['risk_per_contract_usd']==21.9
    if qty:assert r['total_risk_usd']<=budget
    assert args[2]==original


@pytest.mark.parametrize('field,reason',[
    ('max_micro_contracts','mffu_allowances_invalid'),
    ('daily_loss_limit_usd','mffu_daily_policy_unknown')])
def test_unknown_provider_rules_block(case,field,reason):
    args,proof,save=case;proof.pop(field)
    r=cap(case);assert not r['allowed'] and r['reason']==reason


def test_explicit_no_provider_dll_keeps_local_loss_limit_and_one_loss_rule(case):
    args,proof,save=case
    proof.update(daily_loss_policy='NONE',daily_loss_limit_usd=None,daily_loss_source_reference='SYNTHETIC_ACCOUNT_TERMS')
    proof['account_controls']['remaining_daily_loss_usd']=None
    assert cap(case)['allowed']
    proof['account_controls']['losses_today']=1
    assert cap(case)['reason']=='account_wide_one_loss_stop'
    proof['account_controls']['losses_today']=0
    b=args[0]['account']['balance'];b.update(realizedPnL=-550,cashSOD=b['totalCashValue']+550)
    assert cap(case)['reason']=='account_projected_daily_loss_breach'


@pytest.mark.parametrize('change',['unreferenced','non_null_limit','non_null_remaining'])
def test_no_dll_is_never_inferred_from_missing_or_contradictory_data(case,change):
    args,proof,save=case
    proof.update(daily_loss_policy='NONE',daily_loss_limit_usd=None,daily_loss_source_reference='SYNTHETIC_ONLY')
    proof['account_controls']['remaining_daily_loss_usd']=None
    if change=='unreferenced':proof.pop('daily_loss_source_reference')
    elif change=='non_null_limit':proof['daily_loss_limit_usd']=1000
    else:proof['account_controls']['remaining_daily_loss_usd']=1000
    assert cap(case)['reason']=='mffu_daily_policy_contradictory'


def test_provider_local_margin_and_headroom_independently_cap_qty(case,monkeypatch):
    args,proof,save=case
    assert cap(case)['max_quantity']==11
    proof['max_micro_contracts']=8
    assert cap(case)['max_quantity']==8
    monkeypatch.setenv('EXEC_MAX_QTY','6')
    assert cap(case)['max_quantity']==6
    proof['account_controls']['margin_max_new_micro_contracts']=4
    assert cap(case)['max_quantity']==4
    # Cash 48721.05 less floor 48555 less reserve 100 = 66.05 -> 3.
    proof['minimum_balance']=48555
    assert cap(case)['max_quantity']==3
    # Exact boundary is not permitted: 2 * 21.90 = 43.80 headroom.
    proof['minimum_balance']=48577.25
    assert cap(case)['max_quantity']==1
    proof['minimum_balance']=48600
    assert cap(case)['reason']=='mffu_projected_drawdown_breach'


def test_local_floor_can_only_tighten_provider_floor(case,monkeypatch):
    monkeypatch.setenv('DD_FLOOR','48577.25')
    assert cap(case)['max_quantity']==1
    monkeypatch.setenv('DD_FLOOR','47000')
    assert cap(case)['minimum_balance']==48000


@pytest.mark.parametrize('inventory',['positions','orders'])
def test_open_position_or_working_order_blocks_all_quantity(case,inventory):
    case[0][0][inventory]=[{'id':'foreign-manual-position-or-order'}]
    assert cap(case)['reason']=='broker:account_has_positions_or_orders'


@pytest.mark.parametrize('change,reason',[
    ('snapshot','account_snapshot_stale'),('controls','account_wide_controls_stale'),
    ('fees','account_wide_controls_unverified'),('cash_revision','account_wide_controls_balance_mismatch'),
    ('expiry','mffu_allowances_expired_or_unbound'),('contract','mnq_contract_required')])
def test_stale_fee_revision_and_contract_evidence_blocks(case,change,reason):
    args,proof,save=case
    if change=='snapshot':args[0]['read_started_at']-=31
    elif change=='controls':proof['account_controls']['observed_at_ms']-=30001
    elif change=='fees':proof['account_controls']['fees_reconciled']=False
    elif change=='cash_revision':
        b=args[0]['account']['balance'];b['totalCashValue']-=1;b['netLiq']-=1;b['realizedPnL']-=1
    elif change=='expiry':proof['expires_at_ms']=args[3]
    else:args[2]['contract']='NQZ2026'
    r=cap(case);assert not r['allowed'] and r['max_quantity']==0 and r['reason']==reason


def test_no_capacity_is_cached_across_restart(case):
    assert cap(case)['allowed']
    case[1]['account_controls']['losses_today']=1
    importlib.reload(limits)
    assert cap(case)['reason']=='account_wide_one_loss_stop'


def test_current_provider_remaining_can_tighten_local_daily_cap(case):
    case[1]['account_controls']['remaining_daily_loss_usd']=43.8
    assert cap(case)['max_quantity']==1
    case[1]['account_controls']['remaining_daily_loss_usd']=0
    assert cap(case)['reason']=='account_provider_daily_allowance_exceeded'


def test_sizing_reads_fresh_broker_and_final_gate_rereads_limits(native,monkeypatch,tmp_path):
    f=native
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setattr(f.sync,'entry_blocker',lambda **kw:None)
    result=risk.size_for_entry(f.signal,clock=lambda:f.now)
    assert result['allowed'] and result['max_quantity']==11
    # Account allowance changes while AI/entry preparation completes.
    import os
    from pathlib import Path
    path=Path(os.environ['MFFU_RISK_EVIDENCE_FILE']);proof=json.loads(path.read_text())
    proof['max_micro_contracts']=1;path.write_text(json.dumps(proof))
    result=f.validator(dict(f.order))
    assert not result['allowed'] and result['reason']=='mffu_contract_limit'
    assert f.posts==[]


def test_final_poll_reconciliation_failure_cannot_submit(native):
    f=native;original=f.sync.poll;calls=[]
    def changed(**kw):
        result=original(**kw);calls.append(True)
        if len(calls)>=2:f.sync.reconciliation_error='FAILED'
        return result
    f.sync.poll=changed
    f.final_gate.side_effect=lambda order,review:f.validator(order)['reason']
    result=f.t.submit(f.payload(),f.order['order_id'])
    assert not result['sent'] and f.posts==[]
    assert result['reason']=='broker:native_reconciliation_failed'


@pytest.mark.parametrize('allowed',[True,False])
def test_executor_cannot_increase_account_cap_with_legacy_override(native,monkeypatch,allowed):
    import os
    import datetime as dt
    from types import SimpleNamespace
    from unittest.mock import Mock
    from test_builder_crosstrade import functions,ROOT
    f=native
    api=SimpleNamespace(mode=lambda:'LIVE',prepared_entry_blocker=lambda x:None,
        now_ms=lambda:int(f.now*1000),record_quantity=Mock())
    send=Mock(return_value={'sent':True})
    monkeypatch.setattr(risk.builder_crosstrade,'submit',send)
    monkeypatch.setattr(risk,'size_for_entry',lambda s:dict(allowed=allowed,max_quantity=1 if allowed else 0,risk_per_contract_usd=21.9,reason=None if allowed else 'account_capacity_unknown'))
    monkeypatch.setenv('SESSION_SIZE_MULT','');monkeypatch.setenv('PRICE_OFFSET','0')
    ns=dict(os=os,dt=dt,builder_crosstrade=risk.builder_crosstrade,ab_v3_live=api,requests=Mock(),
        execution_policy=SimpleNamespace(entry_blocker=lambda x:None,mode=lambda:'V3_ONLY'),
        live_emit=SimpleNamespace(size_for_budget=lambda *a:(10,10,20,200)),
        guardrails=SimpleNamespace(_exec_route_id=lambda:'test-route'),
        _entry_cancel_after_sec=lambda:60,_signal_reject_after_sec=lambda:60)
    functions(ROOT/'agent.py',{'_exec_order'},ns)
    signal=dict(f.signal,_exec_qty_override=15,_v3_expiry_ms=f.order['expiry_ms'])
    original=tuple(signal[k] for k in ('entry','SL','TP'))
    result=ns['_exec_order'](signal)
    assert result['sent']==allowed
    if allowed:
        body=send.call_args[0][0]
        assert body['quantity']==1
        assert (body['limitPrice'],body['stopLoss']['stopPrice'],body['takeProfit']['limitPrice'])==original
    else:send.assert_not_called()
