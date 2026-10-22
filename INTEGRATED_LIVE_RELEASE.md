# Integrated Directional release — 22 October 2026

Base: be3eb46, including 667545a (Phase 3A), cbe70a5 (2A/2B) and d4ff5ec (2R gross).
This release connects the existing frozen M3/Model0 full-exit policy to a proof-gated native writer. It does not certify broker-paper operation or activate trading.

## Changes in this release

- The original persisted M3/Model0 decision is the sole discretionary full-exit source. Exact order/action/position identity, original rule parity, paper evidence bound to the account/contract, verified executable quotes and current permissions are rechecked after broker reads and before IO.
- Reuse the existing native close journal. Unknown delivery is never blindly resent. A known pre-send veto is durably NOT_SENT and cannot lock out the independent Guard close.
- Native 3R runner entries additionally require proof of the original full-close path, including final entry revalidation.
- Persist the existing manager decision alongside its actual broker-state observation and expose it in /execution/manager. No AI advice or generic research event gains write authority.
- Parse the observed drafted/drafted_consensus ts/eventTs fields for monitoring. Both drafted and collective remain observational regardless of claimed confidence or policy flags.

- Modification confirmation requires the latest report for the exact owned Modify command/order and current version. `rejectReason=Success` is not rejection. `AtExecution` alone never confirms an operator stop modification.
- A rejected management intent is persisted after the complete broker packet validates. Invalid packets still roll back atomically. Restart cannot forget a valid rejection and leave it pending forever.
- Revalidation compares broker facts separately from the poll timestamp. A newly fetched unchanged position can pass; stale duplicate observations cannot preserve a PROTECTED state.
- `/execution/manager` exposes persisted position freshness, unresolved intents and capability restrictions without fetching data or submitting orders. `/execution/readiness` includes this information.
- Guard health includes the effective Directional interlocks and execution validation failures. A configured transport no longer implies “AUTO live & healthy”. Existing alert delivery is unchanged.
- Legacy lifecycle tests now use the production closed-M1 interface, including pre-touch gap rejection. Entry rules, frozen models, thresholds and production permissions are unchanged.

## Capability boundaries

The existing native entry/Guard transport constructs entry OSO brackets and reconciles receipts, fills, original SL/TP, OCO linkage and final accounting. Its Guard full-close/cancel path is unchanged.

The ICT manager observes those actual native records and persists state. Its generic research evaluator and simulated intent lifecycle cover HOLD/REDUCE/EXIT/STOP/TARGET; those generic broker writes remain blocked by `PHASE3B_BROKER_MANAGEMENT_NOT_VERIFIED`. The original frozen strategy full-exit path is separately connected to the existing native close writer. It requires saved paper evidence, account/contract binding, unexpired verification, a current original M3 or Model0 decision, exact position ownership, unchanged protective geometry and verified fresh quotes. Flags alone cannot satisfy this proof.

CrossTrade supports Tradovate PUT `/tv/accounts/{account}/orders/{id}/change` and POST `/tv/accounts/{account}/positions/close`. Support for these individual calls is not proof of an atomic reduction with matching remaining OCO quantities. The Tradovate-specific CLOSEPOSITION documentation says partial closes leave working orders in place. Therefore REDUCE/runner-after-partial cannot safely be implemented by an unverified close-then-resize sequence. Cancel-and-rebracket also has a protection gap. No undocumented atomic endpoint is assumed.

Outstanding advanced integration after an original rule is identified and its broker-paper protocol validated: enable a narrow owned modification writer, coordinate its journal with the native Guard writer and allow only journal-confirmed modified geometry in native reconciliation. The original full-exit runtime is connected in this release; advanced trailing/partial rules are not authorized by that connection. The current native reconciler deliberately expects the original bracket. Simply enabling PUT would cause a changed bracket to be treated as unexpected intervention. These dependencies are not hidden behind an “enabled” flag.

## Original strategy rules preserved

All triggers use completed M1 candles available at decision time; fill minute is excluded. The code and model hashes of v3_frozen_exit.py, v3_boundary.py and v3_runner.py are unchanged.

| Action | Existing condition | Runtime status in this release |
|---|---|---|
| HOLD | No confirmed M3 reason, or Boundary classification RECOVERY | Existing frozen decision; RECOVERY never vetoes native SL or Guard |
| Full EXIT / F2 | Supportive FVG fails by body close and opposing displacement is confirmed | Exact saved M3 reason, before first 2R touch, freshness and broker proof required |
| Full EXIT / B11 | No re-expansion for three completed M1 after supportive FVG retest, plus opposing displacement | Same original M3 dispatch gate |
| Full EXIT / OBFH_EXPANSION | Opposing BOS creates linked FVG of at least 3 points; next bar holds; a later adverse candle makes a new adverse close by >=0.25, body/range >=0.5 and range >= prior ATR20 | Original function, no new thresholds |
| Full EXIT / OBFH_3_CLOSES_10M | Prior three-bar no-expansion warning after >=3 minutes with MFE<0.75R; opposing BOS and linked FVG hold, then three consecutive adverse closes by >=0.25 within ten minutes | Zone body-close invalidation cancels candidate; expiry alone never exits |
| Structural invalidation | The exact F2/B11/OBFH evidence above, not an arbitrary AI opinion | Existing deterministic rule only |
| Optional Model0 runner | At first completed-M1 2R touch, original model selects HOLD_3R only above 0.75; otherwise EXIT_2R, including data fallback | Existing model/hash/year/parity checks; not activated; exact saved decision and separate paper proof required |
| Partial, trailing, break-even | No original approved Directional condition identified | Inactive; no invented management rule or unproved close-and-resize sequence |

M3 dispatch remains inside its existing 10-second signal window, with a maximum 15-second closed-M1 decision age. The original BOS compares a directional close to prior 15-bar extremes with body/range >=0.5; displacement also requires body at least prior 10-bar max and ATR20. These constants are reported from the frozen implementation, not newly proposed.

The writer uses documented POST /tv/accounts/{account}/positions/close for a full owned position. ACK is not a fill. Native reconciliation must prove flat and terminal children before accounting completes. No dynamic stop/target PUT is enabled by this release.

## Required external evidence

1. A separate supported paper account, with no evaluation/payout consequences, identified by account ID and environment and with copy trading disabled. `environment=demo` on an MFFU evaluation is insufficient. Capture real order lifecycle responses, not simulated fixtures. No artificial MFFU orders.
2. Quote-source provenance: contract-specific bid/ask event timestamp semantics, venue BBO status and latency measurements. Current collective/cluster_mean_mid remains observational. The existing adapter can consume a suitable CrossTrade NT8 feed, but the connected NT8 client currently reports disconnected.
3. The user reconfirmed the panel floor 48,161.90 as unchanged. Broker cash/net liquidation is 48,721.05, no positions/working orders: headroom is 559.15 USD, or 459.15 after the existing 100 USD buffer, before new trade risk/costs. Provider daily loss policy/remaining allowance and provider maximum MNQ/margin limits remain UNKNOWN. Configured DAY_LOSS_USD=550 and EXEC_MAX_QTY=15 are application settings, not provider confirmations. autoLiqLevel=-2000 is not an absolute minimum balance. A continuously refreshed account-control source remains necessary.
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

This is an entry configuration target. For the original M3 full-exit feature, only after independently recorded paper acceptance and final authorization, the operator may set V3_EXIT_MODE=real and V3_EXIT_REAL_EXECUTION=true with V3_EXIT_M1_ENABLED=false, V3_EXIT_M2_ENABLED=false, V3_EXIT_M3_ENABLED=true. Existing parity/shadow/staging checks must pass. The approved execution policy must contain broker_workflow_verification.management with full_exit, terminal_children, duplicate_timeout_recovery, a real paper_evidence_reference and verified_at_ms/expires_at_ms; the surrounding evidence must identify the intended account_binding and contract. Do not create this attestation from unit-test fixtures. V3_RUNNER_ENABLED stays false until separate Model0 2R/3R paper acceptance. No permitted partial/trailing configuration is supplied because the original Directional strategy does not define those actions. Until these checks pass, retain V3_EXIT_MODE=shadow and V3_EXIT_REAL_EXECUTION=false. Keep Guard/news/risk limits unchanged. Revalidation still runs per setup; AI approval is not order authorization.

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
