# MNQ Continuation SHORT research v1

## Identity and hypothesis

- Identity: `MNQ_CONTINUATION_HTF_CANONICAL_SHORT_RESEARCH_V1`.
- Asset and timeframe: MNQ, one-minute causal bars; daily thesis fixed at the 18:00 New York trading-day open.
- Style: bearish continuation. Hypothesis: a causally established bearish daily thesis plus registered sell-side liquidity close-through and a canonical bearish displacement/FVG/retest/BOS may precede delivery to an already-open bearish DOL. This is unvalidated; no LONG or Reversal profitability is inherited.
- Mode: independent, broker-inert forward shadow. The immutable `MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1` remains LONG-only and unchanged.

## Entry rules (all required)

1. The original Jade daily thesis source returns `SHORT` for the current trading day. The thesis is based only on completed preceding days, not on same-day hindsight.
2. A registered lower-side level (Asia, London, NYAM, NY lunch, NYPM low; previous-day/previous-week low; or H1 equal low) has formed before the signal. A subsequent minute closes **below** that level. Re-arming requires a close more than the baseline's 3-point buffer **above** it; expiry and deduplication mirror LONG in a separate SHORT detector state.
3. The unchanged canonical detector confirms bearish displacement, event-owned FVG, later retracement/body hold and bearish BOS. Fibo/OTE entry is primary, FVG entry fallback. The same baseline detector environment and risk caps apply.
4. At the order-decision timestamp, the unchanged causal DOL tagger has an `OPEN` bearish pool from a physical-contract lower-side catalog. The DOL must lie strictly below final Entry. No later price action can select or replace it.
5. Final Entry is canonical Entry **minus 1.0 point**, rounded to an MNQ tick. Final SL is the detector's separate structural/FVG-edge/CE hierarchy, with its one-tick anchor buffer and unchanged stop caps. Required initial risk is `SL − Entry > 0`.

## Execution and exit

- Resting limit order activates at the canonical `entry_ms`, expires 10 minutes later (exclusive), and fills at Entry only after the same physical contract's high reaches at least one tick (0.25 point) **above** Entry.
- Entire position exits at the frozen OPEN bearish DOL, structural SL, or physical-contract-roll termination. No manager, partial, break-even, trailing stop or target replacement.
- The fill bar cannot hit a bracket. On later ambiguous bars, SL wins; a stop gap executes at the adverse open above SL. Round-trip cost is $3.50 per MNQ, charged once; point value is $2.00. Short raw R is `(Entry − exit) / (SL − Entry)`.
- This is simulated exposure only. No order reaches the broker; production Reversal and Guard are not changed.

## Risk and filters

- Size in the shadow is one normalized MNQ; no live position-sizing authority is provided by this research module.
- No portfolio-level concurrent-position, daily-loss, or drawdown rule is introduced. Those remain outside this broker-inert detector; the existing Guard is untouched.
- There is no new volatility, time-of-day, news, SMT, or discretionary filter. Missing thesis, causal DOL or valid geometry rejects the candidate.

## Test and status contract

- Software tests cover directionally correct fill, fill-bar exclusion, adverse-first stop and gap handling, target/accounting, SQLite migration, and unchanged LONG regression tests.
- A real-market-data scanner smoke test is permitted to verify candidate generation and causal geometry. It is **not** a profitability test.
- Development profitability, OOS/Validation, SEALED, paper-trading duration and deployable performance thresholds for SHORT: **not established**. They must not be inferred from LONG's Development result. The SHORT hypothesis requires a separately authorized, pre-outcome performance contract before any such claim.
- No live scaling or retirement trigger is defined because this is not authorized for live trading.

## Dependencies and change log

- Reuses the immutable LONG freeze, original Jade daily-thesis source, isolated hashed research `detcore`, and causal DOL ledger. New SHORT logic lives in `continuation_short_engine.py`; integration changes are in `continuation_scan_runtime.py` and `continuation_shadow.py`.
- 2026-09-22: initial exploratory SHORT mirror and forward-shadow integration. No frozen LONG source or hash was changed.
