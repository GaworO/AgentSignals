# Canonical ExecutionPlan deployment - v32.0

## Purpose

`execution_plan.py` converts one A/B detector signal into one immutable, validated order contract.
The broker payload, guard book, shadow resolver and `manage.py` must use this exact plan. No downstream
module should recompute entry, SL, TP, activation time or expiry independently.

## Important: there are no EXEC_PLAN feature flags

In v32 the canonical plan is the normal A/B execution path. Do **not** set `EXEC_PLAN_ENABLED` or
`EXEC_PLAN_SEND`; those names were used in an early draft and are not read by the code.

Control live sending with the existing guard mode:

```text
EXEC_MODE=off       # alerts/shadow only
EXEC_MODE=manual    # approve manually
EXEC_MODE=auto      # full Auto-Executor
```

`AUTO_SUBMIT=1` is only the legacy fallback when `EXEC_MODE` and the saved runtime mode are absent.

## Recommended execution environment

```text
BUFFER_BARS=14000
EXEC_TICK=0.25
EXEC_FILL_THROUGH_TICKS=1
EXEC_FILLBAR_TP=0
EXEC_ADVERSE_FIRST=1
EXEC_TIF=day
FILL_WIN_MIN=10
PARTIAL_AT_1R=0
RAMP_TRADES=10
```

## Required invariants

- `bos_ms`: timestamp of the signal bar.
- `entry_ms` / `active_from_ms`: first theoretically legal time for the pending order.
- Live effective activation: `max(active_from_ms, broker_accepted_ms)` when a broker acknowledgement exists.
- A limit requires trade-through by `fill_through_ticks * tick_size`.
- No target is credited from a favorable extreme that occurred before the fill on the fill bar.
- Stop is evaluated first on an ambiguous OHLC bar.
- `valid_until_ms` is an absolute timestamp, not a number of received bars.
- A multi-leg plan is accepted only when every leg receives HTTP 2xx; partial send triggers cancel.
- Broker callback P&L is authoritative when available.

## Verification

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

Expected for this package:

```text
Ran 18 tests
OK
```
