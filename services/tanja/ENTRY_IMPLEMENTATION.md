# Entry implementation — October 14, 2026

This change implements the user's October 13 corrections and recorded management
choices. It is a local research and paper-execution implementation, not deployed
to Railway. No paid model request or broker order was used to validate it.

## Supported combinations

| Family | Long | Short | Timing |
| --- | --- | --- | --- |
| Manipulation → selected swing break → zone retracement → M1 inversion close | Supported, including a low liquidity sweep before bullish confirmation | Supported, including a high sweep before bearish confirmation | Immediate or optional further retracement |
| H1/H4 objective → M15 displacement/FVG → gap retracement → M1 inversion close | Supported | Supported | Immediate or optional further retracement |
| Existing selected inversion, M1/M2/M3/M5 | Supported | Supported | Immediate or optional further retracement |
| Existing M5 momentum profile | Supported | Supported | Immediate or optional further retracement; retains its explicit M5 swing-close requirement |

Setup family does not select direction. A liquidity sweep and a directional
structure break have different roles. Geometry must agree with the selected
role, and the final inversion close must agree with the trade direction.

## Evidence and entry mechanics

`entry_patterns.py` verifies the referenced chronology, level breaks, zone touch,
HTF objective touch and M15 gap geometry. `entry_rules.py` calculates the strict
inversion close, selected stop/target and price risk. Qualitative context,
confirmation strength and target priority still require explicit assessments.

The pre-confirmation retracement must close before the confirmation candle
starts. This conservative chronology prevents assuming an intrabar event order.
For manipulation, the selected zone can be a formed gap, candle range or complete
range window. For the HTF family, it must be the selected M15 displacement gap.
The M15 middle candle must move in the selected direction; no invented fixed
body-size threshold is attributed to the transcripts.

A post-confirmation retracement is a separate entry choice. Its selected MNQ
limit must improve on the MNQ confirmation close, have a future expiry within
the context lifetime, and retain the explicit cancellation explanation. The
paper simulator waits for a fresh later quote to touch that limit. It cancels
on thesis/structural invalidation, final target reached first, expiry or news
blackout, including while acknowledgement is held. A simulated limit fill never
worsens the limit price. Quote-touch liquidity and acknowledgements are simulator
assumptions; no historical candle is converted into a fill.

## Automatic review connection

`entry_review.py` adds nullable `entry_setup` to the existing structured review.
It includes the family, sequence references, assessments, entry timing, selected
retracement anchor, reversal/continuation classification and optional local
objective. The existing six-call daily limit, spacing and review window remain.
Old v3 records are readable; they do not acquire invented entry selections.

`SelectionObserver` consumes a complete saved selection in its normal background
loop, sizes from current reported account limits, compiles it and applies the
saved-thesis, current news and account guards. Model response availability is
recorded separately from current selection time. Intervening price history must
be complete and must preserve the thesis and objectives. The paper lifecycle
recompiles the selected plan before accepting it and requires a later quote.

The dashboard links a candidate to its specific review/entry and shows entry,
stop and target, or its blocking/missing information. A snapshot without a review
is labelled “Awaiting review of this snapshot.”

## Confirmed user controls

- Local-objective partial: floor(original **filled** contracts / 3). Skip below
  one contract; do not include added lots in that original quantity.
- Two net losing trades per New York calendar date stop new source-mode paper
  entries. Break-even trades do not count; losses need not be consecutive.
- Daily ATR reached blocks new continuation entries, including pending entries
  before fill; open positions keep their existing management.
- Current reported account sizing, cost checks and news restrictions still apply.

The ATR guard now receives a calculated `daily_atr` measurement from stored
closed MNQ candles. The user confirmed five completed full futures sessions;
the convention is an SMA of their true ranges. Missing history still blocks
continuations rather than inventing values. `context_watch.py` also evaluates
each new inversion using the active saved context. See CONTEXT_ATR_UPDATE.md.

## Verification and remaining limits

The tests exercise all eight new family/direction/timing combinations, sequence
errors, wrong-direction confirmation, optional retracement on the older profile,
cancellation/expiry/restart/idempotence, one-third sizing, cumulative two-loss
handling, ATR restrictions and mocked AI → saved selection → observer → paper
entry for long and short. Browser fixtures exercise the eight corresponding
dashboard explanations. Results are recorded in `entry-validation.json`.

These are mechanics tests, not a profitable-strategy backtest or proof of matching
Tanja's discretionary choices. Performance metrics are not measured. New held-out
sessions, replay validation with costs and sustained paper observation are still
needed before a live-readiness claim. The AI still analyzes ES/MNQ; separate NQ
collection does not turn those prices into an NQ analysis. Broker execution,
account-wide trade history, discretionary early exits and complete add/reentry
management are not connected by this change.
