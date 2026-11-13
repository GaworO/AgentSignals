# ICT pending LIMIT review

Operator selection, 13 November 2026: AI may cancel an unfilled entry earlier or
extend once, for at most 20 minutes in total from first submission. This applies
to ICT2022 and IFVG only. It is independent of Directional NO-FILL replacement.

`ICT_PENDING_REVIEW_MODE=ICT_PENDING_AI_20M_V1` enables this separately bounded
pending-order policy. Missing/any other value disables it. This flag grants no
new-entry permission and does not change risk sizing, trade/loss caps or budgets.
The existing policy's `max_pending_lifetime_sec` still caps the INITIAL order at
10 minutes. The additional policy permits only one broker-confirmed extension,
by at most 10 minutes, with a hard 20-minute lifetime. Original setup expiry,
session end, news, mechanical invalidation and account gates remain hard bounds.
An original AI setup valid for only 10 minutes is NOT renewed past that expiry.

## Workflow

1. Use an existing, fresh, reconciled working ICT parent with zero fills and a
   flat account. Include exact immutable order binding, entry/SL/TP/quantity,
   original AI thesis, current deadline and permissible extended deadline in
   the existing scheduled scanner request. No extra AI call is added.
2. AI returns KEEP, CANCEL or EXTEND, with current evidence IDs and a reason.
   KEEP does not change expiry. EXTEND must name the exact offered deadline.
   Missing evidence, stale/invalid output, timeout and exhausted budget cannot
   extend an order. All normal cancellation/expiry checks continue.
3. Review can occur at any normal five-minute scan. Extension becomes eligible
   after five minutes of waiting and must complete BEFORE the current broker
   GTD expires. Starting a review at the exact ten-minute deadline is too late.
4. Recheck the exact parent, fills, account, permissions and mechanical gates.
   CANCEL uses the existing parent-only cancel path. EXTEND durably reserves a
   single attempt, then uses PUT /tv/accounts/{account}/orders/{id}/change with
   only tif=gtd and expireTime. Entry, quantity, SL and TP cannot be changed.
5. ACK is not confirmation. Only an exact current broker GTD version and a
   successful Modify command/report can update the local deadline. A timed-out
   request is never resent. Restart reconciles the reserved attempt. If proof
   is absent before the old deadline, the original cancellation still applies.
6. Any fill ends this AI pending-order path. Partial fills, late fills, native
   SL/TP/OCO, emergency recovery and accounting retain their existing handlers.
   A fill can race a broker change; the change never changes quantity or prices.
   Unknown cancellation remains owned exposure, never permission for a new order.

Example: submit 10:00, initial GTD 10:10, original setup valid to 10:25. A normal
10:05 scan may request extension to 10:20. Broker confirmation before 10:10 is
required. Another scan may cancel sooner if the thesis deteriorates. No second
extension, replacement or resurrection is allowed.

## Operator verification and rollback

After deploying this release, the operator may set the mode above on the same
Agent50k service. `/ict-shadow/state.pending_order_review` exposes mode, bounds
and saved decisions. Native audit records distinguish `ICT_EXTENSION_ATTEMPT`
from `ICT_EXTENSION_CONFIRMED`; `native_actions` contains the durable broker
request. This is not evidence that the broker actually filled the order.

To stop new AI pending changes, remove the mode or set it to `OFF`. Existing
confirmed deadlines remain bounded and mechanically monitored. Do not roll back
code while a modify request or parent is unresolved: reconcile or cancel the
exact parent first. Do not delete protective orders, risk evidence or journals.

Provider capability is documented, not live-tested by this change:
https://crosstrade.io/docs/api/orders/put-change-order
https://crosstrade.io/docs/api/orders/get-order-lifecycle
Tests use synthetic AI and broker responses. No paid test review or account order
is authorized or performed by the tests. No claim of independent live GTD
extension/late-fill validation is made.

## ICT-only AI operation

With `AB_AI_ENABLED=false`, the separate Directional/Q1 reviewer is stopped at
process startup. The independent ICT scanner keeps its own schedule and budget;
stored production running/unknown records and setup queues no longer defer it.
Those records are preserved, never refunded, retried or converted into approvals.
ICT's own in-flight deduplication, cost accounting, freshness and order checks
remain mandatory. Directional/Q1 still require their own exact valid AI approval.
`/ict-shadow/state.production_ai_priority=false` confirms the scheduler choice.
Restoring `AB_AI_ENABLED=true` restores production priority and its existing
unresolved-record checks; investigate old unknown attempts before doing so.
