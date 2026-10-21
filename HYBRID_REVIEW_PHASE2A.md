# Directional Hybrid Review — Phase 2A

This change upgrades the existing Directional review worker, not the scanner,
entry model, execution permissions, strategy policy, broker adapter or Position
Manager. A/B remains governed by existing V3_ONLY shadow routing.

## Effective configuration and budget

Existing `AB_AI_OWNER=AB_DIRECTIONAL`, `AB_AI_TRIGGER=SETUP` now selects effective
`HYBRID` by default. The status API reports this effective trigger. Explicit
`HYBRID` is also supported. `AB_AI_HYBRID_ENABLED=false` retains the old SETUP
review implementation for controlled rollback; it grants no execution rights.
No production environment write is required for the upgrade.

The SQLite transaction reserves each attempt before sending HTTP. Every attempt
counts, including timeout, rejection, refusal, malformed output and a crash after
reservation. Restart does not reset counters or retry unknown/running attempts.
The existing unresolved-attempt latch intentionally suspends further calls that
NY date. No manual deletion/refund is part of this implementation.

Phase 2A uses fixed CONTEXT <=3, SETUP <=7, POSITION reserve 2, hard total <=12.
There are **no transfers** in this phase. Management calls are not implemented;
the reserve cannot be consumed by context or setups. Existing/legacy and known
Tanja calls count toward the hard total and conservatively reduce usable capacity.
Tanja AI must be disabled with known usage. Budget exhaustion persists a blocking
reason for the setup, not an inferred ALLOW.

Reviews run only in the existing NYAM 09:30–11:00 and NYPM 13:30–16:00 NY windows.
All permitted calls are after midnight and before the futures evening rollover,
so their New York calendar date and futures trading date agree. No evening or
management scheduler is introduced. Context calls occur on the first eligible
poll of each session (including a late start) and at most one later structural
event per date. They are not hourly calls. Queued setups have earliest-expiry
priority. One existing worker serializes calls; an in-flight context call cannot
be preempted. Setup expiry always wins over latency. This is deliberately less
concurrent than the two-lane design, preserving the existing unknown-call latch.

## Deterministic context

`directional_hybrid_context.build` is pure and validates close timestamps,
finite OHLC geometry and causal dependency references. The source remains the
existing timestamped ES/NQ/MNQ market packet, not a new feed or bot.

- Strict two-left/two-right pivots become available after the second right bar
  closes, with continuity checks. H1/H4 use two confirmed highs and lows: UP,
  DOWN, MIXED, or UNKNOWN; candle color alone is not directional structure.
- OHLC buy-side/sell-side levels are hypotheses, not observed resting orders.
  Insufficient intervening M1 coverage gives UNKNOWN_COVERAGE, never a certified
  unswept level. Draw candidates and nearest opposing liquidity remain mechanical.
- The latest confirmed M5 high/low define an explicitly mechanical candidate
  range, equilibrium, price and entry location. Discretionary trader anchors
  remain UNKNOWN. Prices outside the range are labelled outside, not discount.
- Closed breaks use the structure known **before** the event: BOS in its known
  direction, MSS against it, otherwise STRUCTURE_BREAK. Sweep/reclaim is distinct.
- MNQ FVGs use three contiguous closed candles; IFVG requires a subsequent close
  through the far edge. Formation, touch and inversion are distinct. NQ source
  gap events retain their NQ identity and never become MNQ executable prices.
- Prior 14-bar simple true-range mean, body fraction, range/ATR and explicit
  mechanical expansion/consolidation labels are context measurements, not new
  entry filters or calibrated ICT confirmation. Daily ATR retains source gaps.
- Alignment uses 30 synchronized closed ES/NQ M1 bars. SMT compares two confirmed
  M5 same-side pivots at exactly paired times; break/nonconfirmation is distinct
  from candle agreement. Missing paired pivots produce UNKNOWN.
- Geometry includes original levels/expiry, gross R and distance to target or
  opposing liquidity. Net R remains UNKNOWN without verified cost/quote inputs;
  the existing final execution validator owns the numeric net-R decision.

Each section retains source packet ID, dependency evidence IDs, observation
cutoff and availability timestamps. Raw bars remain in the exact saved input.
Structure summaries retain the recent hour and latest per kind/timeframe/market;
gap summaries retain latest per market/timeframe/kind, recent and nearby MNQ gaps.
These bounds prevent overflowing the existing 505000-byte packet limit. Oversize
input blocks without spending. If a cached advisory response would overflow the
packet, its exact stored reference is retained with decision_omitted; the complete
fresh setup context is preserved and model sentences are never truncated. Missing per-bar receipt times remain explicitly
UNKNOWN; historical replay uses the previously receipt-filtered source export.

## Lifecycle and final gate

The original scanner and existing DIB/BOS/first-break/minimum-stop eligibility
remain unchanged. Eligible rows enqueue locally; dispatch never calls AI.

The setup review includes the fresh deterministic snapshot and, only when valid,
an advisory cached context review. Generic context cannot authorize a setup.
Context expires after 15 minutes or at session end. Setup validity is the earliest
of original expiry, activation + existing setup TTL (120 seconds), session end.
New material NQ/MNQ structure events invalidate older reasoning conservatively,
including supportive changes. M5+ IFVG inversions and observed M1 takings of
confirmed liquidity (with complete intervening coverage) are material events too.
Same-cutoff data corrections also invalidate it.

Strict output adds seven reason/evidence/missing fields: market_thesis,
liquidity_objective, entry_quality, structural_invalidation,
nearest_opposing_liquidity, rr_assessment, conditional_management_plan.
Exact order ID, direction, setup fingerprint, entry/SL/TP and original expiry
must match. The fingerprint binds contract, activation, validity and frozen setup.
No field changes the original order. Conditional management is advice only.

Input, raw output, parsed decision, prompt version/hash, model, latency, expiry,
review kind, setup lifecycle and validation event are stored in existing SQLite.
After AI, the worker reads fresh market data, checks event changes, data revisions,
continuous post-activation M1 coverage and original SL/TP touches. Model invalidation anchors are also applied to post-response bars before dispatch;
an invalidated context response is never reused. These candles
are validity evidence, **never executable quotes**. The cache must have been read
since process startup and within 15 seconds; packet cutoff must be <=120 seconds
old. Restart cannot reuse a stale cached ALLOW. Changed geometry tombstones the
job, including changes during an in-flight review; a later reply cannot revive it.

The existing dispatcher rechecks routing, eligibility and expiry. After slow
account/thesis/quote reads, `execution_validation.check` rechecks the latest exact
AI state and identity, then all existing quote freshness/provenance, price limit,
original geometry, gross/net R, risk and broker gates. Collective stays
observational. Unverified quotes, unknown risk or absent approved execution policy
still block. No broker-side protection is newly certified by these tests.

## October 21 DIB diagnosis

- `ABDIR_SHORT_512d6a4921b4899f9e39`: 01:42 NY, AL+DIB, entry 29847.5,
  SL 29867.45, TP 29807.6, break 1.
- `ABDIR_SHORT_f2384fd84623fea77f09`: 04:56 NY, NYLL+DIB, entry 29769.6,
  SL 29786.25, TP 29736.3, break 10.

Both have `eligible=false`, `forward_eligible=false`, and recorded
`V3_DIB_DISABLED` under `AB_DIRECTIONAL_NO_DIB_BOS50_V1`.
`ab_directional_entry_rules.evaluate` excludes the DIB category before reading
BOS geometry or direction-execution flags. Both were also outside AI NY windows;
the first would separately fail first-break eligibility. Disabled LIVE flags and
the old deployed revision are real operational facts, but do not explain this
primary DIB rejection. No configuration defect was found here; DIB is not enabled.
Corresponding CONTINUATION rows are a separate shadow family, not these orders.

## Acceptance, deployment and rollback

The test suite uses mock transports only; a real October 20 10:16 NY LONG is
replayed against the existing receipt-filtered historical market reconstruction.
Its exact mocked WAIT is stored and blocks the real resume_reviews path. Separate
LONG/SHORT ALLOW tests use mock dispatchers, never brokers. Production review
behavior remains unverified until a genuine saved new setup review exists.

Deploy this commit to existing Railway Agent50k, after comparing saved and
effective execution settings. Do not change execution flags. Check
`/execution/readiness.commit`, `/ab/ai-context.trigger=HYBRID`, hybrid_budget,
owner and unchanged interlocks. Export a genuine review through the existing
operator-authenticated setup endpoint only when an eligible new setup occurs.
A deployment request or Git push is not proof that the new process is running.

Rollback: redeploy the previously verified commit a0db9e2 with the same execution
interlocks; SQLite changes are additive and preserve old columns. Alternatively,
set only AB_AI_HYBRID_ENABLED=false for old exact-SETUP semantics with this code,
using a separately authorized controlled deployment. Never erase review/budget
rows or reset unknown dispatches. Check the running commit and flags afterward.

Phase 3 prerequisites remain verified executable quote provenance, approved
execution/cost limits, current MFFU allowances and broker protective-order tests.
Advanced Position Manager and real-money activation are explicitly out of scope.
