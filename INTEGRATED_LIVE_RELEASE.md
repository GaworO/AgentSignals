# Integrated Directional release — 22 October 2026

Base: 667545a (Phase 3A), including cbe70a5 (2A/2B) and d4ff5ec (2R gross).
This is a reconciliation/visibility hardening release, not certification of Phase 3B live management.

## Changes in this release

- Modification confirmation requires the latest report for the exact owned Modify command/order and current version. `rejectReason=Success` is not rejection. `AtExecution` alone never confirms an operator stop modification.
- A rejected management intent is persisted after the complete broker packet validates. Invalid packets still roll back atomically. Restart cannot forget a valid rejection and leave it pending forever.
- Revalidation compares broker facts separately from the poll timestamp. A newly fetched unchanged position can pass; stale duplicate observations cannot preserve a PROTECTED state.
- `/execution/manager` exposes persisted position freshness, unresolved intents and capability restrictions without fetching data or submitting orders. `/execution/readiness` includes this information.
- Guard health includes the effective Directional interlocks and execution validation failures. A configured transport no longer implies “AUTO live & healthy”. Existing alert delivery is unchanged.
- Legacy lifecycle tests now use the production closed-M1 interface, including pre-touch gap rejection. Entry rules, frozen models, thresholds and production permissions are unchanged.

## Capability boundaries

The existing native entry/Guard transport constructs entry OSO brackets and reconciles receipts, fills, original SL/TP, OCO linkage and final accounting. Its Guard full-close/cancel path is unchanged.

The ICT manager observes those actual native records and persists state. Its rule evaluator and simulated intent lifecycle cover HOLD/REDUCE/EXIT/STOP/TARGET, but its real broker writer remains blocked by `PHASE3B_BROKER_MANAGEMENT_NOT_VERIFIED`. No environment switch can certify these operations.

CrossTrade supports Tradovate PUT `/tv/accounts/{account}/orders/{id}/change` and POST `/tv/accounts/{account}/positions/close`. Support for these individual calls is not proof of an atomic reduction with matching remaining OCO quantities. The Tradovate-specific CLOSEPOSITION documentation says partial closes leave working orders in place. Therefore REDUCE/runner-after-partial cannot safely be implemented by an unverified close-then-resize sequence. Cancel-and-rebracket also has a protection gap. No undocumented atomic endpoint is assumed.

Outstanding code integration after broker-paper protocol validation: enable the narrow owned modification writer; coordinate its journal with the native Guard writer; allow only journal-confirmed modified geometry in native reconciliation; connect approved causal management rules to the runtime event dispatcher. The current native reconciler deliberately expects the original bracket. Simply enabling PUT would cause a changed bracket to be treated as unexpected intervention. These dependencies are not hidden behind an “enabled” flag.

## Required external evidence

1. A separate supported paper account, with no evaluation/payout consequences, identified by account ID and environment and with copy trading disabled. `environment=demo` on an MFFU evaluation is insufficient. Capture real order lifecycle responses, not simulated fixtures. No artificial MFFU orders.
2. Quote-source provenance: contract-specific bid/ask event timestamp semantics, venue BBO status and latency measurements. Current collective/cluster_mean_mid remains observational. The existing adapter can consume a suitable CrossTrade NT8 feed, but the connected NT8 client currently reports disconnected.
3. Current MFFU minimum balance, daily allowance policy, contract/margin limits and account-wide activity/control evidence. The old DD_FLOOR and an autoLiqLevel of -2000 are not a current absolute broker floor. Account controls must remain current; a one-time manually populated file is not a permanent solution.
4. A measured and approved execution policy with max quote/signal age, entry tolerance, spread/slippage/volatility bounds, verified fees and 2R gross minimum. Do not fill unknown numerical limits with examples from unit tests.
5. Railway build availability for this workspace. Existing service Online is not evidence of deployment availability.

## Paper acceptance sequence

Use the existing service and route against the separately authorized isolated paper account. Wait for a genuine original Directional setup; do not create artificial financial trades. Record the original geometry, AI request/response, quote and risk evidence, native command, ACK and all fills. Prove linked SL/TP quantities equal the remaining position. For each separately supported modification, prove exact commandId -> Replaced report -> current working order version and unchanged sibling protection. Test rejection, timeout without blind resubmission, disconnect and restart recovery. Exercise the existing Guard against the owned paper position and confirm flat plus all children terminal. Partial close/resize requires a supported protection-preserving protocol before it can be tested or enabled. Re-run the acceptance checks after changing the protocol.

## Controlled activation — not to apply yet

Deploy the exact tested release with current interlocks unchanged. Verify actual running code and effective values, persistent volume, fresh closed M1 data, current broker account, saved genuine HYBRID review, verified quote source, risk evidence and paper protection transcript. Any missing check means no activation request.

Only after the checks pass and the user explicitly authorizes final activation, the operator applies the existing Directional entry settings together:

| Setting | Intended activated entry value |
|---|---|
| EXEC_STRATEGY_POLICY | V3_ONLY |
| BUILDER_ORDER_PROVIDER | crosstrade |
| AB_AI_ENABLED / V3_AI_REVIEW_REQUIRED | true / true |
| AB_AI_OWNER / AB_AI_TRIGGER | AB_DIRECTIONAL / HYBRID |
| AB_AI_HYBRID_ENABLED / AB_AI_MAX_CALLS_PER_DAY | true / 12 |
| LIVE_EXECUTION_ENABLED / EXEC_KILL_SWITCH | true / false |
| V3_ENTRY_ARMED | 1 |
| AB_DIRECTIONAL_LIVE_LONG / AB_DIRECTIONAL_LIVE_SHORT | 1 / 1 |
| AB_V3_MODE | LIVE |
| AB_ENTRY_ARMED / AB_MANAGER_MODE | 0 / shadow |
| AB_V3_EXCLUSIVE_ROUTE | 1 |
| DIRECTIONAL_EXECUTION_POLICY_FILE | validated account/contract policy on persistent volume |
| MFFU_RISK_EVIDENCE_FILE | verified current account evidence on persistent volume |

This is an entry configuration target, NOT a complete active-manager configuration. No exact safe active-manager setting exists until the outstanding writer/protocol/rule integration and paper acceptance are complete. Keep V3_EXIT_MODE=shadow, V3_EXIT_REAL_EXECUTION=false and V3_RUNNER_ENABLED=false until those features are separately proven. Keep Guard/news/risk limits unchanged. Revalidation still runs per setup; AI approval is not order authorization.

## Emergency stop and rollback

1. Use the existing authenticated Guard halt/operator controls to block new entries immediately; confirm the active process acknowledges the halt. Merely saving a Railway variable does not change the running process until redeployed.
2. Reconcile broker positions/orders directly. Keep broker SL/TP working. If needed, use the broker's verified emergency close manually and verify flat with no working residual orders. Do not set the master LIVE_EXECUTION_ENABLED=false while relying on application protective closes: it also blocks writes used for exits.
3. Persist EXEC_KILL_SWITCH=true, V3_ENTRY_ARMED=0, both Directional direction flags=0 and AB_V3_MODE=SHADOW before the next deployment. Do not arm A/B.
4. Redeploy the previously verified image/commit to the same service only with entries blocked. Preserve the persistent volume and reconcile outstanding IDs before allowing any dispatch. Never restore an old database over a newer order ledger; use pre-deployment backups only for controlled recovery with an explicit broker reconciliation.
5. If Railway cannot redeploy, use the running application's authenticated halt and broker-native controls. Do not assume a queued rollback has taken effect.

## Primary API references

- https://crosstrade.io/docs/api/orders/put-change-order
- https://crosstrade.io/docs/api/positions/post-close-position
- https://crosstrade.io/docs/webhooks/commands/close-position
- https://crosstrade.io/docs/tradovate/order-types-and-exits
- https://crosstrade.io/docs/tradovate/webhooks
- https://crosstrade.io/docs/api/market/get-quote
- https://status.railway.com/
