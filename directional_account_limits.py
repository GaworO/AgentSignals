"""Account-specific MFFU allowance proof; unknown or expired evidence blocks.

No plan-table starting floor is substituted for today's trailing drawdown floor.
The evidence file is operator-supplied from the account panel/provider statement.
"""
import json
import math
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


def check(snapshot, binding, order, now_ms, cost_per_contract):
    path=os.environ.get('MFFU_RISK_EVIDENCE_FILE')
    if not path:return 'mffu_current_allowances_missing'
    try:
        proof=json.loads(Path(path).read_text())
        if (proof.get('provider')!='MFFU' or proof.get('account_binding')!=binding
                or not proof.get('source_reference') or proof.get('verified') is not True
                or not proof['observed_at_ms']<=snapshot['read_started_at']*1000<=now_ms<proof['expires_at_ms']):
            return 'mffu_allowances_expired_or_unbound'
        for key in ('minimum_balance','daily_loss_limit_usd','max_micro_contracts'):
            n=proof.get(key)
            if type(n) not in (int,float) or not math.isfinite(n) or n<=0:
                return 'mffu_allowances_invalid'
        if type(proof['max_micro_contracts']) is not int:return 'mffu_allowances_invalid'
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
        risk=qty*(abs(order['entry_price']-order['stop_price'])*2+cost_per_contract)
        if min(b['netLiq'],b['totalCashValue'])-risk<=proof['minimum_balance']:
            return 'mffu_projected_drawdown_breach'
        loss=min(0,b['realizedPnL'])+min(0,b['openPnL'])
        # The existing tighter local Guard limit also applies account-wide.
        local=float(os.environ['DAY_LOSS_USD'])
        if not math.isfinite(local) or local<=0:return 'account_daily_limit_unknown'
        if risk>=min(local,proof['daily_loss_limit_usd'])+loss:
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
        for key in ('remaining_daily_loss_usd','cash','equity'):
            if type(controls.get(key)) not in (int,float) or not math.isfinite(controls[key]):return 'account_wide_controls_unknown'
        if abs(controls['cash']-b['totalCashValue'])>.01 or abs(controls['equity']-b['netLiq'])>.01:
            return 'account_wide_controls_balance_mismatch'
        daily_n=int(os.environ['DAY_LOSS_N'])
        if daily_n<0:return 'account_daily_limit_unknown'
        if daily_n and controls['losses_today']>=daily_n:return 'account_wide_one_loss_stop'
        streak_n=int(os.environ.get('LOSS_STREAK_N','3'))
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
        if risk>=controls['remaining_daily_loss_usd']:return 'account_provider_daily_allowance_exceeded'
        return None
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        return 'mffu_allowances_unavailable'
