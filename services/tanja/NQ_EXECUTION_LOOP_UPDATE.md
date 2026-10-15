# ES/NQ analysis and the continuous loop — October 14, 2026

This implements the user's corrected points: analyze **ES and NQ**, price
execution on **MNQ**, and connect the quote-driven execution lifecycle. These
are local source changes. No deployment, paid review or broker order was made.

## Analysis migration

The application now defaults to `TANJA_ANALYSIS_SYMBOL=NQ`. ES/NQ supply the
context, SMT, setup sequence, inversion confirmation and thesis invalidation.
Separate MNQ evidence supplies entry, protective stop and final target prices.
NQ prices are never copied into MNQ orders and no basis offset is assumed.
All three closed-minute feeds must arrive before a new snapshot is queued.

NQ jobs use a new `nq_jobs` table. Existing `jobs`, candles, candidates and AI
audits are preserved. Historical MNQ approvals cannot authorize new NQ plans.
`TANJA_ANALYSIS_SYMBOL=MNQ` explicitly selects the old research path; it is no
longer the application default. A standalone `Store` still defaults to the
legacy path for old offline replays. The six-call budget and review window
remain unchanged and prior attempts still count toward the day's budget.

The retained SMT wire names `reference_mnq`/`current_mnq` are historical schema
names. In NQ packets their referenced windows must belong to NQ; validation
rejects MNQ windows. The prompt states this distinction explicitly.

ES/NQ retain up to 80 candles per timeframe. MNQ retains up to 32 priced candles
per timeframe plus known windows/opens; it does not add a competing inversion,
pivot or gap feature catalogue. Full stored MNQ history still supplies daily ATR.
This keeps the tested three-market packet inside the unchanged 500,000-byte AI
request budget. Oversized packets still fail closed. Older execution anchors
outside retention remain unavailable instead of being estimated or copied from NQ.

Immediate entry uses the matching MNQ confirmation candle close. Dynamic stop
rules resolve the corresponding MNQ candles by exact start, timeframe and close
clock. For optional retracement, use an explicit MNQ anchor or the matching MNQ
confirmation open. An NQ gap-edge price is rejected as an MNQ limit. Missing
MNQ history leaves the execution plan incomplete. Thesis invalidation remains
on NQ and is checked independently from MNQ stop and target boundaries.

The dashboard labels NQ observations and displays ES/NQ analysis coverage. The
review chart shows MNQ order levels only on the MNQ chart, never on NQ candles.

## Continuous paper path

`TANJA_TRADING_LOOP_MODE=paper` attaches the normal saved-plan observer to
`PaperLifecycle` in a background loop. It consumes fresh authenticated quote
events, reserves a new plan once, waits for a later quote, simulates entry,
partial acknowledgements and protective exits, and restores state on restart.
It never manufactures intrabar fills from a closed candle or sends an order.

Current news/account guards are rechecked before a paper entry and before a
pending fill. ATR exhaustion blocks new continuations, not open-position
management. NQ thesis invalidation requires a fresh NQ quote before an NQ-plan
fill. The original filled size remains the one-third partial base. Explicit
structural swing selections are still required for discretionary source-mode
trailing; this update does not invent new swing choices or add-on quantities.

Quote processing is idempotent. The exact input is saved before processing;
repeated polling does not replay a quote with different news, ATR or clocks.
The simulator uses integer-second event clocks, next-event acknowledgements,
configured costs and quote-touch liquidity assumptions. It is not broker fidelity.

## Actual broker loop is blocked with the available connection

The user confirmed that only the TradersPost connection is available.
[TradersPost's FAQ](https://docs.traderspost.io/docs/additional-information/faq)
states that its public integration is webhook-only. A signal receipt does not
supply quotes, account exposure, order states, fills or account-wide trade history.

The [current order reference](https://docs.traderspost.io/docs/core-concepts/webhooks)
also explains that a partial reduction cancels protection covering the original
whole position. Keeping it with `cancel:false` can instead leave oversized
orders. This build therefore does **not** submit partial-exit signals and claim
the runner is protected. A broker adapter must reconcile fills and safely resize
or replace the protection before automated broker entries can be enabled.

`TANJA_TRADING_LOOP_MODE=broker` reports these missing interfaces and sends no
orders. Its status is `LIVE_QUOTES_REQUIRED`, `BROKER_FEEDBACK_REQUIRED`, or
`BROKER_MANAGEMENT_ADAPTER_REQUIRED`. It is an integration boundary, not a
working live broker executor. Entry intents can be prepared durably for review;
they have `test:true`, remain `draft_only`, and have no automatic HTTP sender.

Completing this item requires an actual broker feedback/order-management
connection for the intended account. A second TradersPost webhook, an HTTP
success, a manually reported balance or a paper fill cannot replace it. The
direct [Tradovate API](https://api.tradovate.com/) is one documented interface;
account access must be established separately. No API permission is inferred
from the user's existing TradersPost subscription.

## Broker-bridge intake contract

There is no broker bridge bundled or connected. An external authorized bridge
can send normalized actual broker events to `POST /api/broker-feedback`, using
`Authorization: Bearer <TANJA_BROKER_FEEDBACK_TOKEN>`. This is our intake endpoint,
not a TradersPost API/callback URL. Never put its token in a URL or repository.

Configuration is private:

```text
TANJA_TRADING_LOOP_MODE=off
TANJA_BROKER_ACCOUNT_ID=<exact account identifier>
TANJA_EXECUTION_CONTRACT=<explicit MNQ contract>
TANJA_BROKER_FEEDBACK_SOURCE=<configured broker bridge identity>
TANJA_BROKER_FEEDBACK_TOKEN=<separate random secret>
TANJA_PAPER_SLIPPAGE_POINTS=0.25
TANJA_FEE_PER_CONTRACT_SIDE=0.50
```

Costs above are explicit paper conventions, not verified actual commission.
An exact broker contract must match the MNQ execution-price series and roll
settings before any future live adapter uses it. Continuous chart settings are
not proof of that match. Existing immutable ticker bindings are preserved.

Each event contains exactly `schema_version:1`, `event_id`, increasing
`sequence`, `account_id`, configured `source`, `kind`, `observed_at` (UTC seconds)
and `data`. Quotes require symbol ES/NQ/MNQ, contract, bid, ask and last prices;
quotes older than two seconds or a mismatched MNQ contract are rejected.

Snapshots contain `complete:true`, `scope:entire_broker_account`, history_start,
history_end, account, positions, orders, fills and closed_trades. See
`broker_feedback.validate_snapshot` for exact required fields. The bridge must
attest genuinely complete all-strategy history through the current time and
aggregate partial exits into complete position episodes, with final fees, before
reporting a closed trade. This service cannot prove an upstream omission merely
from a completeness assertion. It does not estimate losses from signal prices.

Two **net losing completed trades across the entire account**, closed on the
current New York calendar date, block new entries. Net break-even episodes do
not count. Other instruments/strategies count. Stale/incomplete snapshots leave
the count unavailable, rather than displaying zero. IDs and finalized net P&L
are immutable; conflicting retries cannot rewrite a loss as a win.

Intake and dashboard routes retain separate authentication. Private broker
events stay in the runtime SQLite database and are excluded from upload bundles.

## Verification

Synthetic tests cover eight family/direction/timing combinations with distinct
NQ/MNQ prices, wrong-series anchors, missing execution bars, six feed-arrival
orders, legacy-audit isolation, new inversions under saved NQ context, stale
quotes, NQ invalidation, complete account-wide net losses, authentication,
conflicts, and the continuous paper enter/partial/restart/exit sequence. No real
broker integration, live order, TradingView compilation or strategy-performance
validation is claimed by these checks.
