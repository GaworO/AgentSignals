# Directional recovery: confirmed FVG wick rule

The scanner captures causal anchors; the existing AI worker coordinates cancellation
proof, one paid review reservation and a unique replacement through the existing
Guard/native route. Recovery remains disabled by default and needs separate final
activation after account and broker checks. Tests use mock AI and broker responses.

## Stop source

`FIRST_FVG_CANDLE_WICK_V1` uses the first chronological candle of the selected
three-candle FVG. All three candles must be closed, contiguous, on the same
timeframe and physical MNQ contract, and available at the review cutoff.

- LONG: first candle low minus `SL_ANCHOR_BUF`.
- SHORT: first candle high plus `SL_ANCHOR_BUF`.
- The existing default buffer is 0.25 points. It must be positive and tick-aligned.
- Explicit second/third-candle mappings are rejected, not silently corrected.
- Missing candle proof stays UNKNOWN. `fvg_bar` alone is insufficient.
- New BOS/FVG also requires the original scanner's causal BOS/displacement and
  structural-source evidence. CONFIRMED_SCANNER_SWING_V1 binds the new impulse
  swing instead of the first FVG candle. Missing evidence rejects the alternative.

AI evaluates whether that frozen invalidation and entry are justified. It can
ALLOW, WAIT or REJECT; it cannot choose a different stop candle or price geometry.
Plan and prompt versions are v3; older saved plans cannot be reused as new approvals.

## Prices and sizing

FIBO uses exactly 50% of the original saved impulse anchors, without the original
entry offset. FVG entries use exactly the selected gap midpoint. Off-tick
midpoints are blocked; no rounding rule has been authorized. Original Directional
setup detection and initial-entry geometry remain unchanged.

TP = entry + 2 × (entry − SL), for either direction. Risk per MNQ is
abs(entry − SL) × 2 USD plus configured costs/slippage. The existing reconciled
account-risk engine sizes whole contracts against 0.5% equity, headroom, exposure,
margin, contract caps and the one-loss rule. Unknown account constraints block.
Recovery requires explicitly configured `EQUITY_PERCENT` with `0.5`; it does not
fall back to a fixed USD budget or enable that configuration in production.

## Recorded candle example, simulated review

19 October 2026, LONG setup 10:18 New York, MNQZ2026:

| Field | Value |
|---|---:|
| Selected FVG candle timestamps | 10:13 / 10:14 / 10:15 NY |
| First candle low, confirmed by user | 29,736.00 |
| Buffered SL | 29,735.75 |
| Original saved impulse anchors | 29,802.25 / 29,837.75 |
| FIBO recovery LIMIT | 29,820.00 |
| Stop distance | 84.25 points |
| Gross risk / MNQ | $168.50 |
| Fixed 2R TP | 29,988.50 |

With illustrative costs/slippage of $3.24/MNQ and historical equity $48,721.05,
the rounded-down 0.5% budget is $243.60, so this particular plan fits 1 MNQ
($171.74 total risk), before all other account gates. This is not a fixed
one-contract policy, a current account verification, or an actual AI approval.

The recorded FVG midpoint 29,792.625 is off-tick: DEEP_FVG is blocked. The second
or third candle as SL source is blocked with `FVG_SOURCE_POLICY_MISMATCH`.
Historical NO_FILL confirmation was recorded after the recovery deadline; this
example establishes price calculations, not permission for a historical order.

## Existing lifecycle safeguards

The journal reserves one recovery review only after authenticated original intent,
confirmed terminal cancellation, zero fills, flat broker positions/orders, fresh
broker evidence and settlement checks. It shares the existing SETUP allocation
and 12-call daily budget and gives queued originals priority. WAIT/REJECT/timeout
are terminal. Selection is advice, never authorization to submit.

The hard entry deadline remains original activation + 20 minutes. A late/partial
fill takes precedence over the deadline and requires protection/reconciliation.
AI response binding, fresh context and all account, Guard, broker and execution
policy gates must still be validated at eventual replacement dispatch. Runtime wiring is present in directional_recovery_runtime.py. A restart consumes an
interrupted review without replay; uncertain dispatch is never retried. All original
entry gates remain mandatory. The 20-minute deadline does not close filled trades.
