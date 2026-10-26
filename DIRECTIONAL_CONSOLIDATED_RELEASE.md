# Consolidated Directional release — 2026-10-26

Restores the current lineage through 76bf6d8 and recovery 4b91e7b after Railway
accidentally built obsolete branch codex/directional-readiness-20261020 (d0d2c83).
No entry or broker permission is enabled by this release.

## Integrated path

Original causal scanner (entry rules unchanged) freezes physical candle/impulse
references. The existing ContextSupport worker and shared SQLite journal own
original and recovery reviews. Recovery is disabled unless
AB_DIRECTIONAL_NO_FILL_RECOVERY=true. Registered original native intents only:
10-minute cancel, terminal broker reconciliation, zero exposure, 15-second
settlement evidence, one reserved SETUP review, exact selected plan, fresh
context and parent checks, existing dispatcher/Guard/account/OSO route.
20 minutes from original activation is the final new-entry deadline. Filled
positions retain protection. Timeout, uncertain submission and restart never
resend a paid request or uncertain replacement. There is no third AI review.
The model cannot change geometry. All three alternatives use causal data;
NEW_BOS requires an eligible event from the original scanner. First FVG candle
wick or explicitly bound scanner swing must be judged valid ICT invalidation.

## Quantity and production configuration

AB_DIRECTIONAL_RISK_MODE=EQUITY_PERCENT and AB_DIRECTIONAL_RISK_PCT=0.5 read current
broker equity, floor the USD budget to cents and whole-contract quantity down.
Stop risk per MNQ = absolute entry-to-structural-SL distance * $2. Policy estimate
adds $2.24 round-trip and two ticks ($1) stop slippage. This is an estimate,
not a guaranteed maximum loss. Account floor/reserve, daily loss/one-loss,
margin/capacity, exposure and verified broker scope remain independent gates.

Deployment-stable paths:
DIRECTIONAL_EXECUTION_POLICY_FILE=/app/directional_live_policy.json
MFFU_RISK_EVIDENCE_FILE=/app/mffu_account_evidence.json

Both files are intentionally blocking drafts: approved=false / verified=false.
The policy's 40 ceiling is not a verified provider entitlement. Account capacity40 is recorded as user-confirmed; provider/margin verification
is incomplete in account evidence. The historical one-contract broker acceptance
cannot be converted into verified 40-contract protection by editing a number.
No artificial fixed-one quantity is used in the risk calculation; incomplete
account/broker evidence currently permits zero new entries.

Required integration-only config corrections: AB_AI_TRIGGER=HYBRID (fixed
3 CONTEXT / 7 SETUP / 2 POSITION; no transfer; hard12 including external usage),
AB_DIRECTIONAL_NO_FILL_RECOVERY=false and the two file paths above. Existing
order/Guard/manager permissions and other settings are preserved. No paid AI
call is manually forced. Original setups have priority over recovery capacity.

## Evidence and limitations

2026-10-26 authenticated CrossTrade GET: equity/cash 48721.05; positions/orders
empty; realized/open P&L zero. Historical 5-MNQ A/B fill set has gross -231,
fees9.50, net-240.50. This is not a new Directional/recovery broker test.
Margin snapshot is a last-change record (2026-10-02), not capacity entitlement.
User-reported floor48161.90 implies raw room559.15 / buffer-adjusted459.15;
current provider rule validity, quantity, margin and complete history checkpoint
remain unverified. Unknowns are not filled with assumptions.

At illustrative equity48721.05, budget243.60. SHORT29108/SL29135/TP29054:
54+3.24=57.24 per contract, budget-only4 contracts (228.96). LONG29820/
SL29735.75/TP29988.50:168.50+3.24=171.74, budget-only1. Both require all other
gates; actual allowed quantity is zero until missing evidence is resolved.

878 tests +36 subtests: broker protection, fill/cancel/restart races, exact AI,
account/risk, policy, Guard and recovery runtime. Additional isolated scanner
suite:18 tests +13 subtests. One pre-existing dashboard navigation assertion
(dolrevmgr alias absence) is obsolete and not an execution check; recorded,
not hidden or fixed by altering unrelated UI. Import scanner tests using the
production continuation_scan_runtime bootstrap (isolated research detcore).
All AI and broker order operations in tests are mocks. Real API checks are GET.

## Activation and rollback

Current saved user switches may already read LIVE/armed. They are not evidence
of authorization: policy/account/broker checks remain closed. User acceptance of dynamic0.5% risk and candle-only architecture is recorded.
After current account rules, accounting coverage and broker scope are verified,
follow DIRECTIONAL_OPERATOR_ACTIVATION.txt for user-controlled activation. Do not approve
JSON or enable recovery as part of code deployment. A fresh exact ALLOW is still
required at each genuine original/recovery opportunity.

Emergency new-entry stop: operator sets EXEC_KILL_SWITCH=true and V3_ENTRY_ARMED=0;
leave LIVE_EXECUTION_ENABLED available for existing protective reconciliation.
Do not delete/cancel protective children or close filled trades merely because
an entry expired. Existing owned pending entries reconcile/cancel via the normal
recovery path; uncertain state blocks exposure. Restore code only from a verified
consolidated archive while flat/reconciled, preserving persistent journals. Do
not reconnect the obsolete branch or deploy d0d2c83. Risk/evidence files remain
blocked during rollback unless separately reverified and authorized.
