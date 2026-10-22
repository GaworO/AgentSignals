# Directional ICT Position Manager — Phase 3A

Local implementation on top of `d4ff5ec`, 22 October 2026. **Not deployed. No
production flags, Guard settings, permissions, paid AI calls or broker orders
were changed by this phase.** A/B routing and Directional entry rules are unchanged.

This is a persistent observation and deterministic simulation foundation. The
real CrossTrade adapter **unconditionally refuses every new management write**,
including when LIVE flags are enabled. Completing a simulator lifecycle does
not establish broker-side protection on the MFFU account.

## 1. Original manager audit

| Capability | Existing implementation / evidence | Phase 3A addition |
|---|---|---|
| Entry and original target / stop | Native CrossTrade bracket, owned entry/child IDs and broker fills; original fixed target remains at broker. User's accepted entry objective is 2R **gross**, with net costs still reported. | Observes full TP and SL completion without generating another close order. |
| Persistent position | `v3_position_manager.py` has assumed/shadow positions and a broker-confirmed path; `builder_crosstrade.py` owns durable order/fill reconciliation. | Account-bound `ict_positions`, `ict_position_events`, `ict_management_intents`, consuming the same broker reconciliation batch. |
| Early exits | Frozen M1/M2/M3 exit library and Boundary classification. F2, B11, B13/MSS and opposing BOS→FVG hold rules exist. | Explicit closed-candle invalidation event plumbing, disabled research option. Existing frozen decisions are not rewritten or relabeled ICT approval. |
| Runner | Frozen Model0, 18 features, strict score >0.75, pre-touch information, year/dense-feed restrictions; legacy 2R/3R and Boundary research. | Simulated post-partial target-extension intent with exact command confirmation. This is a separate research option, not promotion of historical Model0 into a new live rule. |
| Partial take-profit | No verified Directional native partial plus linked bracket-resize lifecycle. | Explicit integer reduction quantity/price; simulated reduction, protected remainder and runner/SL paths. Real action blocked. |
| Break-even | No verified native Directional break-even lifecycle in the original route. | Disabled research trigger; stop to actual average entry only if tick-valid, tightening and behind executable side. Explicitly **not fee-neutral break-even**. |
| Structural trailing | No verified native Directional structural trail. | Confirmed swing event, timestamped level, no loosening; ACK does not change stored stop. Real action blocked. |
| Session end / Guard | Existing account Guard and native queued exits remain authoritative. | Observes their owned close fills. Optional simulated session-end proposal does not replace the independent Guard. |
| A/B manager | `ab_position_manager.py` is a separate original A/B manager, with different ownership. | New manager rejects registration of any strategy other than `AB_DIRECTIONAL`. No A/B live permissions added. |

The frozen exit library includes supportive FVG failure with opposing
displacement (F2), lack of re-expansion with opposing displacement (B11),
re-expansion failure with opposing MSS (B13), MSS and opposing BOS/FVG-hold
expansion or three-close conditions. Historical configurations differ; these
are not interchangeable with a new arbitrary ICT invalidation level.

Archived `v3_exit_parity.json` records 939 checks and zero stated signal
mismatches, M3 PF 1.0656 and Model0+M3 PF 1.1143 on its historical sample. These
are archived parity/research figures, **not live performance or the same cohort
as the 580-trade comparison below**. Frozen model weights and thresholds were
not changed.

Original runtime switches include `V3_EXIT_MODE=shadow`,
`V3_EXIT_REAL_EXECUTION=false`, `V3_RUNNER_ENABLED=false`, exit parity/shadow/paper
attestations and the master entry/risk controls. The first three are safe code
defaults; this phase did not read or mutate Railway. The previous saved
preflight (22 October) showed kill=true, entry armed=0, both Directional flags=0,
mode=SHADOW and AUTO_SUBMIT=0. That is **prior evidence**, not a newly verified
effective production configuration. The new adapter block is independent of
all those variables and cannot be removed by setting an environment flag.

## 2. Persistent broker-authoritative state

Each registered record freezes setup ID, direction, dated contract, original
entry/SL/TP/quantity and the three native bracket IDs. It tracks actual weighted
entry, observed remaining quantity, original risk in points/USD, current stop
risk, working protection, immutable fills, fee revisions and gross/net realized
P&L. MNQ uses the existing 0.25 tick and USD 2 per point convention; this module
is not a generic instrument engine.

Original timestamped DOL/boundary observations are copied through the existing
causal evidence extractor. Unavailable fields are `UNKNOWN`. The protective SL
is not automatically called an ICT thesis-invalidation level. Research levels
carry source, known time and expiry. No AI advice is consumed as execution
authority, and this module makes no AI request.

Unrealized R uses the liquidation side of an explicitly verified fresh quote.
Observed high/low and MFE/MAE R use closed bars whose entire interval follows the
first fill. They are **observed excursion only**, not reconstructed complete
intratrade extremes. Broker updates clear unrealized R until another valid
quote is provided. Missing fees keep net P&L unknown; a fee/cash disagreement
does not inherit the previous reconciled net figure.

On restart, stored intents are recovered and current broker positions, orders
and fills must reconcile before any proposal can proceed. Account/contract,
entry ownership, native parent/OCO linkage, order side, stop/target type and
remaining quantity must agree. Over-sized protection, unrecognized child IDs,
unexplained manual changes, extra exposure or conflicting fill data require
reconciliation. A broker packet validates atomically: one correct stop update
cannot partially commit when another leg in that packet is invalid.

This phase recognizes the original native close journal (Guard/session/early
exits) as well as TP/SL fills. New multi-child repair trees are not guessed:
they require an explicit Phase 3B mapping. Cached terminal lifecycle evidence
may accompany a fresh account read; working orders must come from the current
native reconciliation batch. The observation hook adds no broker HTTP calls.

## 3. State machine and action contract

| State | Meaning / transition |
|---|---|
| RECONCILIATION_REQUIRED | Awaiting fill/protection evidence or a failed invariant. No new management proposal. |
| PROTECTED | Actual open quantity agrees with fills and both linked working legs. |
| HOLD | Explicit no-action decision or an outstanding stop/target modification. |
| REDUCE_PENDING | Durable reduction request; ACK alone leaves quantity unchanged. |
| RUNNER | Reduction filled and target extension confirmed by the exact command report and matching order. |
| EXIT_PENDING | Exit request or original native close is outstanding; position remains open until broker evidence proves flat. |
| CLOSED | Owned fills net to zero, entry is terminal and protective/close orders are terminal. Fees may still be unknown. |

Decision order is explicit invalidation → session-end → one partial → optional
runner → confirmed-swing trail → optional break-even → HOLD. All research
options default inactive; the example configuration is not loaded by production.
A partial must leave at least one contract. A stop must tighten, remain behind
the current liquidation price and not increase the original total stop risk.
Rules expire at decision time, not merely at their old candle timestamp.

`on_event()` requires a confirmed closed event, source/contract, caller-supplied
event/quote freshness limits, verified quote timestamp semantics, a fresh
account-risk verdict for the same binding and fresh reconciled broker state.
Partial entry fills are protected but cannot start research management while
the entry remains working. Final submission re-reads broker state and
re-evaluates the proposal. Intent reservation is committed before simulated IO;
duplicates and UNKNOWN outcomes are never blindly resent.

`v3_position_manager.observe_native()` is wired to the existing native broker
reconciler. **There is no automatic production candle→research-rule scheduler**
and no live management writer in Phase 3A. The typed event entry point is
exercised by the tests/demo. This keeps undefined thresholds inactive instead
of silently inventing live rules. Guard's existing independent exit path is
unchanged, including during a market-data outage.

## 4. CrossTrade support and the remaining execution boundary

The Tradovate API documents order changes for price/quantity. A change response
contains a command ID; lifecycle command/report evidence is required before
treating the modification as successful. A version row or HTTP success alone
is insufficient. [Change-order documentation](https://crosstrade.io/docs/api/orders/put-change-order).

Tradovate also documents full/partial position close by quantity or percentage.
The manager prepares matching read-only operation plans. The available
documentation does not prove the atomic linked SL/TP resizing and protection
handoff required here. [Close-position documentation](https://crosstrade.io/docs/api/positions/post-close-position).

Native brackets are broker-hosted, while CrossTrade can repair protection after
partial fills. Such repair may introduce additional order identities. This
phase accepts only relationships it can prove, rather than assuming the first
three IDs cover every future repair. [Tradovate order and exit documentation](https://crosstrade.io/docs/tradovate/order-types-and-exits).

Implemented plans: stop change, target change, single-order quantity change,
partial close and full close. **All plans have `allowed=false`; `submit()` and
`require_write()` refuse every operation on the real adapter.** Only the
in-memory test adapter can submit. Native linkage/working state are read;
safe broker mutations remain for Phase 3B.

Crucially, the existing Phase 2B reconciler deliberately rejects unrecognized
modifications and changed original bracket geometry. Before activating any
Phase 3A intent, Phase 3B must teach that reconciler to accept only the exact
owned, confirmed modification journal. Otherwise a valid manager change would
look like manual intervention. This change has **not** been smuggled into the
observation-only patch.

## 5. Lifecycle verification

`tests/test_directional_position_manager.py` has 29 passing cases covering all
12 requested lifecycle categories: LONG/SHORT TP and SL, partial→runner,
partial→remaining SL, trailing ACK/confirmation, structural exit, restart,
partially filled entry, duplicate/unknown requests, rejected stop, quantity
mismatch and manual broker activity. Additional cases cover stale evidence,
expired options, fee revisions, atomic validation and original native closes.

The focused Phase 2A/2B/manager/A/B regression run and original entry-rule suite
passed **344 tests plus 29 subtests**, zero failures/errors. Results are saved
as `tests.xml` and `entry-tests.xml` in the artifact directory.
All test broker operations are mocked; external networking is disabled by the
offline test runtime. There are no paid AI calls or real broker orders.

The expanded historical suite is separately retained as `expanded-tests.xml`.
It has 18 failures in unchanged `test_v3_boundary_runtime.py` at its obsolete
one-second feed helper. Production `ab_v3_live._bar()` rejects that feed by
design; `legacy-check.json` proves both files match base `d4ff5ec`. These tests
were not deleted, marked xfail or used to justify restoring one-second input.
This delivery does not claim the entire legacy repository suite is green.

Reproduce the illustrative lifecycle with:

```sh
python tools/phase3a_manager_demo.py /tmp/phase3a-lifecycle.json
python tools/phase3a_research.py /path/to/research_v3_boundary_runner_shadow_20261006 /tmp/phase3a-research.json
```

`lifecycle-demo.json` labels every quote, candle, fill, risk verdict and command
as simulated. The example takes one of two contracts at 1R and extends the
other to 3R. **This is not a chosen strategy rule or performance result.** Atomic
bracket resize exists only in the simulator; real operation remains blocked.

## 6. Historical comparison: 580 identical entries and quantities

Metrics are recomputed from saved complete policy ledgers, not a newly run
candle replay. Fees follow the original research convention, USD 3.50 round
trip per contract. Drawdown is realized exit-cashflow drawdown from zero; it is
not MFFU trailing drawdown or mark-to-market drawdown.

| Archived policy | Net PF | Net win rate | Expectancy USD/trade | Closed-trade drawdown USD | Net USD |
|---|---:|---:|---:|---:|---:|
| Original fixed 2R | 0.7872 | 31.72% | -33.09 | 22,030.50 | -19,191.50 |
| Boundary early exit | 0.7614 | 29.14% | -34.81 | 22,511.50 | -20,189.50 |
| Selective 3R branch only | 0.7291 | 29.48% | -43.86 | 27,281.00 | -25,436.50 |
| Boundary + selective runner | 0.7008 | 26.90% | -45.58 | 28,279.00 | -26,434.50 |
| Partial TP | Not measured | — | — | — | No comparable causal outcome ledger |
| Structural trailing | Not measured | — | — | — | No fixed swing rule and comparable outcome ledger |

On this saved cohort, the tested changes **worsen** net PF and expectancy;
none establishes a profitable policy. The later archive contains one filled
trade: fixed/runner-only -206 USD; Boundary variants -100 USD. A single trade
cannot establish an improvement.

Limitations: 2019–2022 data evaluated retrospectively using a model trained
later, repeatedly inspected samples, no untouched holdout; 33 recorded trades
with open-period M1 gaps and three same-minute SL/TP ambiguities; idealized
first-2R-touch runner execution; extended positions can overlap (maximum two)
without replaying portfolio admission. Source SHA-256 hashes, training
separation, protocol and detailed metrics are in `research.json`. Do not
combine these figures with the separate 939-case frozen exit parity evidence.

## 7. Rule decisions deliberately left open

- Partial price/level, integer sizing and whether it applies to one-contract trades.
- Conditions selecting a runner, its liquidity target, maximum holding period
  and timing before the original broker target can fill.
- Swing confirmation definition, trailing offset and minimum adjustment step.
- Break-even trigger and whether fees are included; average-entry stop alone
  does not mean net break-even.
- Exact structural invalidation evidence and whether to await a closed M1 or M5.
- Quote/event freshness, spread, permitted execution slippage and how to handle
  safe exits when market data is unavailable. Test constants are not live defaults.
- Interaction priorities with existing M3, Guard, scheduled flatten and partial
  entry/repair orders. Existing Guard/session policy remains untouched.

## 8. Exact Phase 3B requirements

1. A supported isolated broker paper account, correct account/environment and
   dated contract, Pro API access, verified executable bid/ask and timestamp
   semantics. Unverified `collective` remains observational.
2. Record authentic entry/partial fill/SL/TP/OCO topology, including repaired
   children and reconnects. Prove same-account ownership, quantity and terminal
   sibling cancellation from broker lifecycle evidence.
3. Capture actual stop/target/quantity change command IDs, report status,
   rejection fields and resulting working order. Test timeout/unknown outcome,
   duplicate callback and restart without blind resubmission.
4. Prove a safe partial-close sequence with protection continuously covering
   remaining quantity and without oversized orders. If no such sequence or
   atomic operation can be verified, keep partials disabled. Never infer it
   from one successful HTTP response.
5. Extend the original Phase 2B geometry validator for exact owned management
   intents only, not arbitrary price changes. Coordinate all writers through
   one journal/serialization boundary; Guard must not race a reduction or trail.
6. Select/validate the missing research rules using a causal replay and an
   adequate later, untouched sample. Preserve original Directional entries.
   Current archived results do not justify activating a runner or early exit.
7. Wire closed-candle events to the existing service scheduler only after those
   rules are specified; refresh verified quote, spread/limit checks, current
   account-wide realized/unrealized risk, broker orders and Guard before each
   potential write. Verify behaviour on data/connection/account uncertainty.
8. Paper-test all 12 lifecycles with broker evidence; include process restart,
   manual intervention, disconnect with a working protective stop, unexpected
   fill, fee revision and an emergency stop. Alert on UNKNOWN/RECON states.
9. Back up journals, deploy with entries/management still inhibited, verify
   effective configuration and observation only. Live activation is a separate
   explicit approval after evidence passes; no environment flag alone removes
   the Phase 3A adapter prohibition.

Rollback must retain broker-hosted protection and all journals. Inhibit new
management intents, reconcile in-flight commands, then revert code while
preserving original Guard/owned exits. Do not restore an older database or
resend UNKNOWN commands. A kill switch by itself is not evidence that the
account is flat.
