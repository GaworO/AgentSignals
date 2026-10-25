"""Account-specific MFFU allowance proof; unknown or expired evidence blocks.

No plan-table starting floor is substituted for today's trailing drawdown floor.
The evidence file is operator-supplied from the account panel/provider statement.
"""
import json
import math
import os
import re
from pathlib import Path
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime
from zoneinfo import ZoneInfo


def read_static_evidence():
    path=os.environ.get('MFFU_RISK_EVIDENCE_FILE')
    if not path:raise ValueError('mffu_current_allowances_missing')
    proof=json.loads(Path(path).read_text())
    if not isinstance(proof,dict):raise ValueError('mffu_allowances_invalid')
    return proof


def read_evidence():
    proof = read_static_evidence()
    if proof.get('account_controls_source') == 'CROSSTRADE_RECONCILED':
        import directional_account_reconciliation as reconciliation
        import builder_broker_sync
        import hashlib
        state = reconciliation.state()
        digest = hashlib.sha256(builder_broker_sync.encode(proof).encode()).hexdigest()
        if not state.get('verified') or state.get('rules_sha256') != digest:
            raise ValueError('account_wide_reconciliation_unverified')
        proof = dict(proof, account_controls=state['controls'])
    return proof


def daily_policy(proof):
    """NONE is explicit evidence, never inferred from zero/missing API fields."""
    mode=proof.get('daily_loss_policy','LIMITED')
    value=proof.get('daily_loss_limit_usd')
    if mode=='NONE':
        if value is not None or not proof.get('daily_loss_source_reference'):
            raise ValueError('mffu_daily_policy_contradictory')
        return None
    if mode!='LIMITED' or type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ValueError('mffu_daily_policy_unknown')
    return value


def floor_and_reserve(proof):
    floor=float(proof['minimum_balance'])
    local=float(os.environ.get('DD_FLOOR',floor))
    buffer=float(os.environ.get('DD_BUFFER','100'))
    reserve=float(os.environ.get('SETUP_GROUP_FLOOR_RESERVE_USD','100'))
    if not all(math.isfinite(v) and v>=0 for v in (floor,local,buffer,reserve)):
        raise ValueError('drawdown_reserve_unknown')
    return max(floor,local),max(buffer,reserve)


def check(snapshot, binding, order, now_ms, cost_per_contract, *, evidence=None):
    path=os.environ.get('MFFU_RISK_EVIDENCE_FILE')
    if not path:return 'mffu_current_allowances_missing'
    try:
        proof=read_evidence() if evidence is None else evidence
        if (type(cost_per_contract) not in (int,float) or not math.isfinite(cost_per_contract) or cost_per_contract<0):
            return 'transaction_costs_unknown'
        if not 0<=now_ms-snapshot['read_started_at']*1000<=30000:
            return 'account_snapshot_stale'
        if any(not isinstance(snapshot.get(k),list) for k in ('positions','orders')):
            return 'account_inventory_unknown'
        if snapshot['positions'] or snapshot['orders']:
            return 'broker:account_has_positions_or_orders'
        if (proof.get('provider')!='MFFU' or proof.get('account_binding')!=binding
                or not proof.get('source_reference') or proof.get('verified') is not True
                or not proof['observed_at_ms']<=snapshot['read_started_at']*1000<=now_ms<proof['expires_at_ms']):
            return 'mffu_allowances_expired_or_unbound'
        for key in ('minimum_balance','max_micro_contracts'):
            n=proof.get(key)
            if type(n) not in (int,float) or not math.isfinite(n) or n<=0:
                return 'mffu_allowances_invalid'
        if type(proof['max_micro_contracts']) is not int:return 'mffu_allowances_invalid'
        try:provider_daily=daily_policy(proof)
        except ValueError as exc:return str(exc)
        if not re.fullmatch(r'MNQ[HMUZ]\d{4}',str(order.get('contract',''))):
            return 'mnq_contract_required'
        b=dict(snapshot['account']['balance'])
        # Actual Tradovate cash-balance API uses USD/SOD suffixed fields.
        # Conflicting aliases are unknown risk, not permission to pick one.
        for canonical,aliases in {
            'cashSOD':('totalCashValueSOD','cashSODUSD'),
            'fundsInTransit':('fundsInTransitUSD',),
            'fundsInTransitAppliedToCash':('fundsInTransitAppliedToCashUSD',),
        }.items():
            values=[b[k] for k in (canonical,*aliases) if k in b]
            if not values or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                return 'account_wide_pnl_unknown'
            if any(abs(v-values[0])>.01 for v in values):return 'account_wide_pnl_unreconciled'
            b[canonical]=values[0]
        keys=('netLiq','totalCashValue','realizedPnL','openPnL','cashSOD','fundsInTransit','fundsInTransitAppliedToCash')
        if any(type(b.get(k)) not in (int,float) or not math.isfinite(b[k]) for k in keys):
            return 'account_wide_pnl_unknown'
        if b['fundsInTransit'] or b['fundsInTransitAppliedToCash']:
            return 'account_cash_transfer_unreconciled'
        if abs(b['netLiq']-b['totalCashValue']-b['openPnL'])>.01 or abs(b['totalCashValue']-b['cashSOD']-b['realizedPnL'])>.01:
            return 'account_wide_pnl_unreconciled'
        qty=order['quantity']
        if type(qty) is not int or not 0<qty<=proof['max_micro_contracts']:
            return 'mffu_contract_limit'
        entry,stop=order['entry_price'],order['stop_price']
        sign={'LONG':1,'SHORT':-1}[order['direction']]
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (entry,stop)) or sign*(entry-stop)<=0:
            return 'structural_stop_invalid'
        risk=qty*(abs(entry-stop)*2+cost_per_contract)
        floor,reserve=floor_and_reserve(proof)
        if min(b['netLiq'],b['totalCashValue'])-risk<=floor+reserve:
            return 'mffu_projected_drawdown_breach'
        loss=min(0,b['realizedPnL'])+min(0,b['openPnL'])
        # The existing tighter local Guard limit also applies account-wide.
        local=float(os.environ['DAY_LOSS_USD'])
        if not math.isfinite(local) or local<=0:return 'account_daily_limit_unknown'
        if risk >= (min(local,provider_daily) if provider_daily is not None else local)+loss:
            return 'account_projected_daily_loss_breach'
        # Volatile controls must be provider/account-wide evidence, not a
        # static plan floor or a guessed count from this bot's trade ledger.
        controls=proof.get('account_controls')
        if not isinstance(controls,dict):return 'account_wide_controls_unknown'
        age=now_ms-controls.get('observed_at_ms',-1)
        if not 0<=age<=30000:return 'account_wide_controls_stale'
        if (controls.get('source_reference') is None or controls.get('verified') is not True
                or controls.get('contract')!=order.get('contract')
                or controls.get('ny_day')!=datetime.fromtimestamp(now_ms/1000,ZoneInfo('America/New_York')).date().isoformat()
                or controls.get('fees_reconciled') is not True):
            return 'account_wide_controls_unverified'
        for key in ('losses_today','consecutive_losses','margin_max_new_micro_contracts'):
            if type(controls.get(key)) is not int or controls[key]<0:return 'account_wide_controls_unknown'
        for key in ('cash','equity'):
            if type(controls.get(key)) not in (int,float) or not math.isfinite(controls[key]):return 'account_wide_controls_unknown'
        if abs(controls['cash']-b['totalCashValue'])>.01 or abs(controls['equity']-b['netLiq'])>.01:
            return 'account_wide_controls_balance_mismatch'
        daily_n=int(os.environ['DAY_LOSS_N'])
        if daily_n<0:return 'account_daily_limit_unknown'
        if daily_n and controls['losses_today']>=daily_n:return 'account_wide_one_loss_stop'
        streak_n=int(os.environ.get('LOSS_STREAK_N','3'))
        if streak_n<0:return 'account_loss_streak_limit_unknown'
        if streak_n and controls['consecutive_losses']>=streak_n:
            # A foreign account loss cannot escape the existing local cooldown.
            last=controls.get('last_loss_closed_ms')
            hours=float(os.environ.get('LOSS_STREAK_COOLDOWN_H','24'))
            if type(last) not in (int,float) or not math.isfinite(last) or not 0<last<=now_ms:
                return 'account_loss_history_unknown'
            if not math.isfinite(hours) or hours<=0 or now_ms-last<hours*3600000:
                return 'account_wide_loss_streak_stop'
        # Guard separately enforces its existing consecutive-loss cooldown.
        if qty>controls['margin_max_new_micro_contracts']:return 'account_margin_or_exposure_limit'
        remaining=controls.get('remaining_daily_loss_usd')
        if provider_daily is not None:
            if type(remaining) not in (int,float) or not math.isfinite(remaining):return 'account_provider_daily_allowance_unknown'
            if risk>=remaining:return 'account_provider_daily_allowance_exceeded'
        elif remaining is not None:
            return 'mffu_daily_policy_contradictory'
        return None
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        return 'mffu_allowances_unavailable'


def equity_percent_mode():
    mode=os.environ.get('AB_DIRECTIONAL_RISK_MODE','FIXED_USD')
    if mode not in ('FIXED_USD','EQUITY_PERCENT'):
        raise ValueError('directional_risk_mode_unknown')
    return mode=='EQUITY_PERCENT'


def trade_budget(snapshot, legacy_budget):
    """Server-configured percentage of current equity; includes all trade costs.

    Snapshot freshness/account reconciliation must still pass the caller's
    existing checks. Never use start balance, webhook values or a cached cap.
    """
    if not equity_percent_mode():return legacy_budget
    equity=snapshot['account']['balance']['netLiq']
    if type(equity) not in (int,float) or not math.isfinite(equity) or equity<=0:
        raise ValueError('current_equity_unknown')
    pct=Decimal(os.environ['AB_DIRECTIONAL_RISK_PCT'])
    if not pct.is_finite() or not Decimal('0')<pct<=Decimal('0.5'):
        raise ValueError('directional_risk_percent_invalid')
    # Floor cents: displayed/authorized budget never rounds above 0.5%.
    return float((Decimal(str(equity))*pct/100).quantize(Decimal('.01'),rounding=ROUND_FLOOR))


def capacity(snapshot, binding, order, now_ms, cost_per_contract, budget):
    """Exact cost-inclusive cap, never a minimum-one override or a stop change.

    External evidence is loaded once and reused for all checks in this decision.
    Existing exposure is rejected (the original single-position strategy).
    """
    result=dict(allowed=False,max_quantity=0,order_id=order.get('order_id'),reason='account_capacity_unknown',
                direction=order.get('direction'),entry_price=order.get('entry_price'),
                stop_price=order.get('stop_price'),target_price=order.get('target_price'))
    try:
        sign={'LONG':1,'SHORT':-1}[order['direction']]
        distance=sign*(order['entry_price']-order['stop_price'])
        if not math.isfinite(distance) or distance<=0:return dict(result,reason='structural_stop_invalid')
        result.update(stop_distance_points=distance,stop_risk_usd=distance*2,
                      gross_2r_target=order['entry_price']+sign*2*distance)
        proof=read_evidence()
        budget=trade_budget(snapshot,budget)
        if type(budget) not in (int,float) or not math.isfinite(budget) or budget<=0:
            return dict(result,reason='strategy_risk_budget_unknown')
        minimum=dict(order,quantity=1)
        reason=check(snapshot,binding,minimum,now_ms,cost_per_contract,evidence=proof)
        if reason:return dict(result,reason=reason)
        cap=int(os.environ['EXEC_MAX_QTY'])
        if cap<=0:return dict(result,reason='local_contract_limit_unknown')
        b=snapshot['account']['balance'];c=proof['account_controls']
        dec=lambda v:Decimal(str(v))
        per=abs(dec(order['entry_price'])-dec(order['stop_price']))*2+dec(cost_per_contract)
        floor,reserve=floor_and_reserve(proof)
        room=min(dec(b['netLiq']),dec(b['totalCashValue']))-dec(floor)-dec(reserve)
        daily=daily_policy(proof);local=dec(os.environ['DAY_LOSS_USD'])
        daily_room=min(local,dec(daily)) if daily is not None else local
        daily_room+=min(Decimal(0),dec(b['realizedPnL']))+min(Decimal(0),dec(b['openPnL']))
        strict=lambda amount:max(0,int((amount/per).to_integral_value(rounding=ROUND_CEILING))-1)
        capacities=dict(strategy_budget=max(0,int((dec(budget)/per).to_integral_value(rounding=ROUND_FLOOR))),
            drawdown=strict(room),local_and_provider_daily=strict(daily_room),
            provider_max=proof['max_micro_contracts'],local_max=cap,
            available_margin=c['margin_max_new_micro_contracts'])
        if daily is not None:capacities['provider_remaining']=strict(dec(c['remaining_daily_loss_usd']))
        qty=min(capacities.values())
        result.update(risk_per_contract_usd=float(per),stop_risk_usd=float(per-dec(cost_per_contract)),
            costs_and_slippage_usd=cost_per_contract,strategy_budget_usd=budget,
            risk_basis='CURRENT_EQUITY_PERCENT' if equity_percent_mode() else 'FIXED_USD',
            risk_equity_usd=b['netLiq'],
            minimum_balance=floor,reserve_usd=reserve,usable_headroom_usd=float(room),
            daily_loss_policy='NONE' if daily is None else 'LIMITED',daily_remaining_usd=float(daily_room),
            caps=capacities,checked_ms=int(snapshot['read_started_at']*1000),account_binding=binding)
        if qty<1:return dict(result,reason='risk_budget_below_one_contract')
        reason=check(snapshot,binding,dict(order,quantity=qty),now_ms,cost_per_contract,evidence=proof)
        if reason:return dict(result,reason=reason)
        return dict(result,allowed=True,reason=None,max_quantity=qty,total_risk_usd=float(per*qty))
    except (OSError,ValueError,TypeError,KeyError,OverflowError,ArithmeticError):
        return result
