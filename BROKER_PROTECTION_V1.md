# Directional V1 broker protection

Scope: original LIMIT + structural SL + gross 2R target. No advanced partial,
trailing, runner or new entry rules. A/B stays shadow under V3_ONLY.

## Confirmation and protection

Native placement stores an idempotent local intent before sending one OSO
request. HTTP success and returned order IDs only mean acceptance. The local
strategy has no confirmed position until actual owned fill records, account
position and child orders reconcile. Unknown delivery is never blindly resent.

Protection requires exact account/contract, original entry direction/limit,
known entry/SL/TP IDs, parent/OCO links, opposite-side stop and target, original
prices, stable broker command/version evidence, and Working child quantities
matching remaining filled exposure. For each SL/TP, residual quantity is its
current total orderQty minus its own verified fills; the sibling must independently
cover exactly the same residual position. This only verifies broker fills and
resizing; it does not introduce partial-profit management or resize orders.
Suspended zero-quantity children are not
protection for a filled position. A zero-quantity Working child can be read as
an owned but unprotected order; it never passes the protection check.

Every supplied tif/timeInForce alias must be DAY or GTD. V1 emits GTD bounded
by the original setup expiry. IOC/FOK/GTC inputs are rejected before broker IO;
no alias can hide IOC and convert it into a resting order. This is application
behavior, not certification of the broker's IOC implementation.

## Existing protective recovery, made durable on all reconciliation errors

1. Stop new exposure: the nonterminal native journal blocks all additional entries.
2. Persist native_exit_requests even if malformed/missing broker evidence caused
   an exception before the normal bracket check. Mark protection unverified.
3. Read fresh account and exact owned lifecycle/fill evidence again. If ownership
   or broker state is still uncertain, do not mutate the account. Keep the
   request across restart and record EXIT_RECOVERY_BLOCKED with the reason.
4. Cancel only the attributed remaining entry. A cancellation ACK is not a
   terminal cancellation. Wait for broker terminal status before full closure.
5. Close only the verified owned remaining position through the existing close
   path. Never replace the bracket, widen the stop, or flatten unrelated exposure.
6. A close ACK is not flat. Require actual exit fills, flat account and terminal
   children; then reconcile captured costs and broker cash before releasing the
   journal entry barrier. Partial entry/exit fills remain accounted for.
7. Unique durable cancel/close action IDs prevent duplicate IO after timeout or
   restart. Unknown mutation outcomes require broker reconciliation, not resend.

EXEC_KILL_SWITCH blocks new entries while preserving the existing protective
write path. LIVE_EXECUTION_ENABLED=false blocks all writes; recovery stays
pending for operator action. These permissions are not changed by this release.
Unverifiable ownership/disconnection requires restoration of the data connection
or direct operator inspection at the broker. The system cannot truthfully
promise an automatic close without evidence or connectivity.

`builder_crosstrade.status().recovery_requests` exposes pending recovery.
Repeated identical blocked errors are deduplicated in the native audit journal.
No external alert delivery or real-money permission is introduced here.

## Evidence limits

On Oct 23, only MFFU Evaluation account …5021 / Tradovate demo is connected.
No independent personal paper account is available; NT8 is disconnected. Do not
submit a test to MFFU. Historical Oct 20 A/B records prove a 5-MNQ native LIMIT
fill, linked Working SL/TP, stop fills, TP cancellation and cost reconciliation.
That trade used 3R and is not a paper test of the current Directional 2R release.
An older Directional order has real GTD cancellation/no-fill evidence only.

Independent acceptance needs a separately linked Tradovate simulation account
with no evaluation/payout consequences, funded simulation balance, MNQ execution
access and no copying to consequential accounts. Bind that explicit account and
verify a minimal LIMIT/fill/SL/TP/OCO/close sequence plus error/restart cases.
Mock protocol tests must remain clearly labelled simulation; they do not create
provider-paper attestation. No verification flags are set from this report.

Provider references:
- https://crosstrade.io/docs/api/orders/post-place-order
- https://crosstrade.io/docs/api/tradovate/overview

Deployment evidence, complete saved lifecycle and test results are in the
existing operational audit's protection_step3 directory.
