# Directional CrossTrade execution — Phase 2B

Based on Phase 2A `d0d2c83`. This change does not alter arming variables, original
Directional entries, targets, AI prompts, context calculations or 3/7/2 budget.
No paid model requests or actual account orders were made for this phase.
A/B remains shadow under the existing `V3_ONLY` policy.

## Implemented

- Final validation requires positive quote provenance/timestamp verification,
  rejects an adverse executable side beyond the original LIMIT, and records
  server-side validation inputs and the unchanged gross/net RR calculation.
- The post-AI thesis GET now refreshes the existing hybrid event cache before
  consulting ALLOW. Original liquidity/source/entry evidence is compared to the
  exact saved setup, in addition to geometry, time, state and structural checks.
  This is an integration fix, not a new context calculation or prompt.
- Final risk rechecks current broker inventory and the original execution policy
  after building its risk budget. Unknown account-wide fees, loss history,
  daily remaining allowance or margin/exposure capacity block entries.
- The quote adapter distinguishes a recognizable NT8 response from a verified
  bid/ask event timestamp. Source changes are journaled; collective/drafted
  cannot acquire execution eligibility through a matching configuration label.
- Native lifecycle accepts suspended zero-quantity OSO legs, recognizes protected
  partial positions without promoting them to a full-size strategy position,
  cancels expired entries and residual entries after a partial exit, and retains
  durable cancellation intent across restart. ACK remains distinct from broker
  terminal confirmation. Reconciliation failures revoke current OPEN proof.
- Missing verified protection queues the existing owned-position emergency exit:
  cancel the entry, wait for terminal evidence, prove ownership/quantity, close.
  No replacement brackets, discretionary reductions, trails or runner added.
- Direct native submissions enforce Directional mode, direction and arming as
  well as policy and kill switch; permissions are checked again after slow reads.
  Final risk still runs the actual Guard. No webhook fallback is introduced.
- A fixed native SL/TP path no longer depends on advanced M3/runner model files
  or enabling that manager. Instead it requires explicit provider-paper workflow
  evidence in the approved execution policy. Simulated tests cannot supply it.
  Legacy/runner paths keep their original exit attestations.

## Unchanged RR conflict — operator decision required

`execution_policy.entry_blocker` requires a fixed **2R gross** bracket (runner
is separately gated and excluded here). The existing final gate additionally
requires minimum **2R net**. These cannot both pass with positive costs.
For risk D dollars/contract, commission C and stop slippage S:

- gross RR = target dollars / D;
- net RR = (target dollars - C) / (D + C + S).

Recorded Oct 20 LONG 29718.25 / 29696.50 / 29761.75:
D=$43.50; target=$87; C=$1.90; S=0 -> 2 gross, **1.874449 net**.
A one-tick stop-slippage stress adds $0.50 to the loss denominator -> 1.854031.
Recorded Oct 21 LONG 30020.50 / 29990.75 / 30080.00:
D=$59.50; target=$119; C=$1.90 -> **1.907166 net**.
Using only gross risk in the denominator is a different R-multiple convention.
Do not treat an approximately 1.904R example as universally applicable.

No policy change is made here. A nonzero-cost original fixed-2R setup remains
blocked until the intended gross/net definition is explicitly confirmed and
implemented/tested. The offline full protocol demo uses a clearly labeled
zero *estimated* cost fixture solely to traverse this existing gate; its broker
simulation then charges $1.90 RT per contract. This is not LIVE acceptance.
No SL/TP widening or favourable-quote assumption resolves this contradiction.

## External evidence contracts (not populated or activated)

In the existing approved `DIRECTIONAL_EXECUTION_POLICY_FILE`:

- `quote_verification`: `verified: true`, exact `source: crosstrade_nt8`, dated
  `contract`, `reference`, `timestamp_semantics: bid_ask_event_time`,
  `venue_bbo: true`. Provider confirmation and independent observations must
  support these assertions; response JSON shape alone is insufficient.
- `broker_workflow_verification`: `verified: true`, `simulator_only: false`,
  `provider: CrossTrade`, `surface: tradovate`, exact `contract`,
  `paper_evidence_reference`; `native_oso_oco`, `partial_fill_repair`,
  `restart_recovery`, `net_accounting` must all be true from actual paper evidence.
- Existing approved spread/age/zone/volatility/slippage/cost parameters remain
  required. No production numerical entry thresholds were invented.

The existing `MFFU_RISK_EVIDENCE_FILE` still needs a provider/account-bound current
trailing floor, DLL and contract allowance. It now also requires
`account_controls` with `verified`, `source_reference`, `observed_at_ms`, `ny_day`,
exact `contract`, `cash`, `equity`, `fees_reconciled`, `losses_today`,
`consecutive_losses`, `margin_max_new_micro_contracts`, `remaining_daily_loss_usd`.
Controls must be <=30 seconds old (existing broker freshness ceiling), match
broker cash/equity and represent the whole account, including manual trades.
A triggered loss streak also needs `last_loss_closed_ms`; existing cooldown and
one-loss/day settings remain in force. A static screenshot/floor is not an
ongoing source for these volatile fields. An authoritative feed/export adapter
with complete account coverage is still an external dependency.

## Quote research and support draft

[CrossTrade quote documentation](https://crosstrade.io/docs/api/market/get-quote)
describes NT8 data and its epoch field; it does not certify collective or the
precise age semantics of that feed. The Tradovate API has no standalone quote
endpoint. `cluster_mean_mid` suggests aggregated pricing; it is not proof of
CME BBO provenance. `ts` versus `eventTs` semantics and hidden aggregation delay
remain unverified. Raw timestamps, receipt latency and source are saved.

[API surfaces](https://crosstrade.io/docs/api/overview) distinguish the NT8 add-on
from cloud Tradovate. [Order handling](https://crosstrade.io/docs/tradovate/order-types-and-exits)
distinguishes native protection from CrossTrade's demo deadline/partial-fill
watchers. [Execution details](https://crosstrade.io/docs/tradovate/webhooks)
warn that plain placement is not idempotent and position feeds can lag fills.
The local intent journal therefore never blindly retries ambiguous entry POSTs.

**Draft only — not sent to support:** For MNQ 12-26 our quote response reports
`source=collective`, `pricingBasis=cluster_mean_mid`, `confidence=high`; one prior
response used `drafted`. Are bid/ask exchange BBO observations or derived bounds?
What precisely produces `ts`, `eventTs`, and NT8 `epoch` (bid/ask, last trade,
aggregation, receipt or snapshot time)? Does event age include upstream delay?
What are source fallback, stale/unavailable, sequence and entitlement guarantees?
Can the existing linked Tradovate integration expose timestamped CME BBO without
NT8? Please supply the partial-fill bracket-repair lifecycle, ownership linkage
for replacement children, and the supported disposable paper-test route.

## Evidence, limitations and deployment

Public runtime captures Oct 21 and Oct 22 (06:14 UTC / 02:14 NY) show running `a0db9e23321926ff3f064ec48c9dc779a18a99c6`,
not Phase 2A/2B; `V3_ONLY`, kill=true, entry armed=false, both directions=false,
mode SHADOW, auto-submit=false, manager shadow, runner=false. A/B is shadow.
A genuine **older setup-only** LONG review occurred at 15:49 NY, returned ALLOW
at 15:50:20, expired 15:51; dispatch had already stopped at direction_not_enabled.
It is not proof of the undeployed hybrid version. It is also after the configured
15:30 last-entry cutoff; that safeguard is not changed. Public export lacks the
exact authenticated model input/model version; do not reconstruct it as recorded.

Runtime account snapshot: cash/equity $48,721.05, no positions/working orders,
0 session fills, 3 retained fills; transport reports CLOSED=1 and NO_FILL=1.
Verified broker floor is null. Local day net/losses are 0; local rolling streak=1
with last recorded net loss -$240.50. These are observations, not a verified
account-wide margin, complete fee revision history or current MFFU allowance.
[Builder documentation](https://help.myfundedfutures.com/en/articles/14290805-builder-plan-50k-a-comprehensive-guide)
lists $2,000/$1,500 MLL variants, $1,000 DLL and 40 micros for evaluation; the
actual variant/current threshold must come from this account. Local $550/day,
1 losing group/day, 15 contracts and $250/setup ceilings remain tighter.

The direct provider read was blocked because Railway CLI authentication expired.
After the user reported logging in, an explicit `railway whoami` still returned
Unauthorized; no RAILWAY token environment override was present. Saved variables
and direct provider GETs therefore remain unverified. The public runtime/account
read was refreshed successfully on Oct 22. No credential from chat was embedded
in scripts.
TradingView quote_get was attempted read-only: it returned requested MNQ label
with ES description/~7800 price and no bid/ask. It was rejected as inconsistent,
not compared to MNQ collective. No independent BBO comparison was possible.
Railway's incident update says OAuth sessions were invalidated and builds are
being restored in stages. At Oct 22 06:01 UTC builds were available only for
Enterprise, with deployments sometimes stuck at pre-deploy. No deployment retry
was made in Phase 2B. See https://status.railway.com/.

Paper broker protection, CrossTrade replacement-child schemas on multiple fills,
actual broker latency/OCO behaviour and complete account allowances remain
unverified. Unknown/new child IDs are NOT adopted or called protected. Test
simulator success does not establish provider-paper success. IOC is deliberately
unsupported for the original Directional path and rejected before POST rather
than silently converted to GTD.

## Controlled deployment and rollback (not executed)

1. After Railway recovery and login, read saved *and effective* variables and
   compare with the captured permissions. Stop if a pending environment change
   would arm trading. Snapshot the existing persistent SQLite databases with
   SQLite backup semantics; retain journals, fills, AI attempts and budget state.
2. Deploy the reviewed commit to existing Agent50k service
   `5311ba22-8e59-4197-a1a5-490325e91911` in project
   `26df4462-061e-48e5-ad8a-b364909a1b8f`, environment
   `c479e22f-8661-42d4-870a-6a95354589ad`. Do not deploy a different service.
3. Confirm `/execution/readiness.commit` equals the deployed SHA. Read
   `/execution/quotes`, `/ab/ai-context`, `/continuation/live`, `/guard/data` and
   `/ab/v3/data`. Keep kill=true, V3_ENTRY_ARMED=0, directions=0, V3 mode SHADOW,
   AUTO_SUBMIT=0, `V3_ONLY`, manager shadow and runner disabled.
4. Before asking for LIVE activation: resolve gross/net policy, verify a licensed
   timestamped executable quote source, provide approved numerical policy and
   current account-wide risk evidence, and complete genuine disposable-paper
   native bracket/partial/restart/accounting acceptance. Do not use MFFU Builder
   as a disposable test account. Confirm a genuine saved hybrid review.
5. Only after explicit user approval may the operator enable V3 mode LIVE,
   intended Directional direction flags, AUTO_SUBMIT, V3_ENTRY_ARMED and release
   EXEC_KILL_SWITCH. Keep A/B arm=0, `V3_ONLY`, runner=false and manager shadow.
   Do not extend GUARD_LAST_ENTRY_ET or change Guard/news/loss limits.
6. Rollback: inhibit new entries first (kill=true, V3_ENTRY_ARMED=0), reconcile
   pending requests, actual fills, native SL/TP and account state. A kill switch
   does not itself prove flat. Preserve master write availability for necessary
   owned protective exits while exposure exists. When settled, redeploy the
   captured previous working image (a0db9e2), leaving entries inhibited.
   Keep the volume/journals and current AI budget; never restore an older trading
   DB or resend UNKNOWN intents. Reconcile again before any later reactivation.

The code uses existing tables and new journal states only. Old-version rollback
is safe only while entries are inhibited; new PARTIAL_OPEN/WAITING_ENTRY_CANCEL
and action CONFIRMED states require reconciliation before normal operation.


## Deterministic verification

The focused execution/hybrid/quote/risk/route suites passed **285 tests and 16
subtests**. The original Directional entry-rule suite passed **10 tests and 13
subtests**: total **295 tests + 29 subtests**, zero failures/errors. This is not a
claim that every unrelated historical repository test has been repaired.
`tests/test_crosstrade_phase2b.py` adds 40 parameterized acceptance cases.
No network order request or paid AI request is performed by these tests.

Reproducible offline trace: run `tools/phase2b_execution_demo.py OUTPUT.json`
with the test dependencies installed. It consumes the minimized authentic
`tests/fixtures/phase2b_recorded_setup.json`; all quotes, risk attestations,
AI approval and broker responses are explicitly mocked. The complete valid-cost
entry is BLOCKED; only the zero-estimated-cost protocol fixture traverses all
order lifecycle steps. Simulated protection is not broker-paper verification.

Artifacts are in the user's workspace under
`audit/crosstrade_phase2b_20261021/`: HTML report, execution-demo.json, authentic
public review/dispatch, timestamped runtime observations, and two JUnit reports.
The exact authenticated AI request/raw model response is unavailable; the saved
public parsed response is preserved verbatim and must not be called the raw
provider response.
