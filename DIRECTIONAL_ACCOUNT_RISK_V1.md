# Directional LIVE V1: account risk and quantity

This release changes sizing and risk validation only. It does not arm entries,
change account limits, certify a quote feed, or enable position-management writes.

## Exact quantity

For dated MNQ, point value = USD 2 and tick value = USD 0.50.

- LONG structural stop risk = `(entry - SL) * 2` USD/contract.
- SHORT structural stop risk = `(SL - entry) * 2` USD/contract.
- Total estimated loss/contract = stop risk + validated round-trip commission/fees
  + validated stop slippage ticks * 0.50.
- Strategy quantity = floor(strategy USD budget / total estimated loss/contract).
- Drawdown headroom = min(broker cash, equity) - max(provider current absolute
  floor, configured DD_FLOOR) - max(DD_BUFFER, SETUP_GROUP_FLOOR_RESERVE_USD).
- Quantity must leave strictly positive headroom after estimated loss. Exact
  boundaries at the drawdown or daily limit are rejected.
- Final quantity is the minimum of strategy quantity, drawdown capacity, local
  and applicable provider daily capacity, provider remaining daily allowance,
  account-specific maximum MNQ contracts, EXEC_MAX_QTY and current margin capacity.
- Existing exposure or working orders block this single-position entry path.
  The system does not assume a working order is harmless or automatically net it.
- Quantity zero rejects the setup. There is no minimum-one override and no SL/TP
  change to fit size. Original LIMIT and minimum 2R gross remain separate gates.

`directional_risk_revalidation.size_for_entry` refreshes account state before
native preparation and writes a local `account_position_sizing` trace.
`RiskValidator` repeats the account read, allowance, costs, quantity and Guard
checks immediately before native submission. A lowered cap rejects the already
prepared order; it does not change its approved geometry or silently resubmit.

## Required evidence and units

`MFFU_RISK_EVIDENCE_FILE` is the existing account-bound evidence file. Nothing in
this release creates an approved file from a generic plan table. Required stable
fields: provider=MFFU, exact account_binding, source_reference, verified=true,
observed_at_ms, expires_at_ms, current minimum_balance (absolute USD), integer
max_micro_contracts. Evidence must precede the broker read and remain unexpired.

`daily_loss_policy=LIMITED` requires a positive daily_loss_limit_usd. Legacy
positive-limit evidence remains compatible. `daily_loss_policy=NONE` requires
null daily_loss_limit_usd and an account-specific daily_loss_source_reference.
An absent field, zero, or generic API null does not establish no provider limit.
Even with NONE, local DAY_LOSS_USD and DAY_LOSS_N continue to apply unchanged.

Volatile `account_controls` must be at most 30 seconds old, verified and sourced,
bound to the same NY date and exact contract, and agree with broker cash/equity.
They include fees_reconciled, account-wide losses_today and consecutive_losses,
margin_max_new_micro_contracts, and remaining_daily_loss_usd (null only for NONE).
A breached consecutive-loss rule needs the actual last_loss_closed_ms for the
existing cooldown. Do not populate unknown counts or margin capacity with zero
or a guessed limit. A static manually supplied file is not a continuously fresh
account control feed; it expires closed.

The approved execution policy independently supplies contract-bound round-trip
costs and stop-slippage assumptions. Historical observed fees are evidence for
reconciliation, not a guarantee of future costs. EXEC_MAX_QTY is a local cap;
it is never used as proof of the provider's maximum.

## Reconciliation and restart

The existing AccountSync performs current account/position/order/fill reads.
Its native reconciliation callback must succeed. Cash/SOD/realized P&L and
equity/cash/open P&L must reconcile; transfers or contradictory aliases block.
Existing native intent journals, unattributed fills, delayed fee adjustments and
pending costs continue to block. Guard retains its one-loss, daily-loss, maximum
trades, session/news and execution permission gates. The native submission
reservation permits only its own unsubmitted intent, not another pending order.

No risk approval is cached across restart. Journals persist, but quantity and
volatile evidence must be reread. Missing, stale, contradictory or unavailable
risk evidence returns no permission and zero account capacity.

## External evidence still required for account ending 5021

The Oct 23 panel identifies Evaluation and shows floor USD 48,161.90. Broker
reads confirm cash/equity USD 48,721.05 and flat inventory. Broker watermarks are
empty; the margin snapshot is dated Oct 2 and its -2000 value is not an absolute
current floor. Current account terms must establish the plan variant, provider
DLL (or explicit no DLL), MNQ maximum and usable margin capacity. Current public
Builder 50k rules say USD 1000 soft pause / 40 micros, conflicting with the user's
no-DLL statement; these generic numbers are not installed as verified account
limits. Local limits USD 250/trade, USD 550/day, one loss/day, 15 contracts and
USD 100 floor reserve remain unchanged.

Official references:
- https://help.myfundedfutures.com/en/articles/14290805-builder-plan-50k-a-comprehensive-guide
- https://ninjatrader.my.site.com/tradovate/s/article/Risk-Settings-Tradovate?language=en_US
- https://api.tradovate.com/ (accountRiskStatus/list,
  userAccountPositionLimit/deps, userAccountRiskParameter/deps; separate native
  API access is required unless CrossTrade exposes equivalent evidence.)

No support request, purchase or native Tradovate credential extraction is part
of this release. The connected CrossTrade route has not supplied these limits.
