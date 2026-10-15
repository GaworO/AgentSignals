# 3.2 — separate quotes and account-wide execution accounting

This is a source update, not an activated trading connection. Orders remain
disabled. The existing 3.1 Railway deployment is unchanged by preparing these
files. All tests use synthetic data; no CrossTrade account or paid data service
was contacted during implementation.

## What is implemented

- Independent authenticated `/api/live-quotes` intake for actual ES, NQ and MNQ
  dated-contract bid, ask and last-trade observations. Each component needs its
  real source timestamp. Quotes expire after two seconds. A disconnect, stream
  gap or reconnect invalidates the cache. A selected but stale quote source never
  falls back to candles or the old broker quote bridge.
- A GET-only CrossTrade Tradovate reader, optional background capture and durable
  local history. Pagination is bounded, cursor cycles fail, redirects are refused,
  and errors do not print credentials or account identifiers. Capture is off by
  default. The client has no order routes.
- Separate account/environment and contract-spec bindings. All account instruments
  must have explicit USD contract multipliers and tick sizes; unknown manual
  instruments block the account-wide count. Raw identity-wide session rows are
  retained even when ownership cannot yet be assigned.
- Stable execution IDs deduplicate fills. Later commission/fee observations append
  revisions. Missing history rows never delete local executions. Changed fill
  prices, quantities or ownership require reconciliation rather than rewriting
  historical fills.
- Exact rational P&L arithmetic, weighted average entry prices, additions, partial
  exits and position reversals. A trade finishes when that contract's position is
  flat. Reversal costs are allocated between the closing and new positions.
- Losses use gross P&L minus commission and fees, attributed to the New York
  calendar date of final closure. Break-even does not count. Partials do not each
  count as separate trades. Results cover the account, including other strategies
  and manual trades, when their executions are included and reconciled.
- Optional broker-statement import for gaps and explicit cost confirmation,
  including genuine zero costs. The checked evidence file's SHA-256 is retained.
  Revised covered fills or costs invalidate that reconciliation.
- Unverified coverage, unresolved costs, unknown ownership, stale capture or
  ambiguous ordering of simultaneous fills block new entries, including paper
  entries when this account provider is selected. Existing paper positions still
  receive stop/exit management. Broker management remains disabled.
- Dashboard execution panel explains quote and accounting readiness separately.

## What CrossTrade Pro alone cannot establish

The direct Tradovate API has no market-data feed. A licensed live source or a
properly timestamped desktop exporter is still required. Existing TradingView
closed-minute candles remain useful for analysis, but cannot act as fresh quotes.
The supplied `tools/quote_bridge.py` forwards a chosen producer's normalized JSONL;
it does not obtain a data subscription or implement a vendor-specific feed.

CrossTrade's durable history is periodic and can miss sessions. An exhausted
pagination cursor does not prove completeness. Its new fills may initially show
zero costs, and later corrections can change costs. Consequently, this integration
does **not** automatically declare those costs final or a missing session empty.
Raw session capture helps retain evidence but cannot recover sessions missed before
capture began. Some identity-wide rows lack account IDs; unresolved ownership blocks
entry readiness instead of being silently ignored.

An offline broker statement can reconcile history, not establish continuing live
coverage. A reconciliation window must start at a verified flat inventory before
New York midnight and include every instrument through its cutoff. Its fingerprint
must match the executions and costs, and closing inventory must match the statement.
For live entry gating it expires after five seconds. **CrossTrade capture never
creates or refreshes this attestation.** Therefore CrossTrade's documented history
API alone does not currently unlock fully automatic net-loss gating. A verified
live accounting source with account ownership, coverage and cost confirmation is
still required for that. The deliberately short expiry cannot be bypassed by a
successful API response or heartbeat.

This is an execution/P&L subledger, not a general accounting or tax system.
Realized futures P&L is price difference × quantity × contract multiplier; full
futures notional is not booked as a cash purchase. No actual prop-firm drawdown
allowance, working protection or order reconciliation is inferred from this ledger.

## Private service configuration

Keep these in Railway private variables or a private shell environment, never Git.
Do not paste keys into chat. Retain `TANJA_TRADING_LOOP_MODE=off` and disabled AI
while validating read-only inputs.

```text
TANJA_ACCOUNTING_PROVIDER=crosstrade
TANJA_BROKER_ACCOUNT_ID=<actual numeric Tradovate account ID>
TANJA_CROSSTRADE_ENVIRONMENT=demo
TANJA_CROSSTRADE_ACCOUNT_NAME=<exact CrossTrade account display name>
TANJA_CROSSTRADE_API_TOKEN=<private Pro API token, not webhook secret>
TANJA_CROSSTRADE_CAPTURE_ENABLED=false
TANJA_CROSSTRADE_CAPTURE_INTERVAL=30
TANJA_CROSSTRADE_LOOKBACK_DAYS=7
```

Use the actual linked environment; prop-firm accounts often use `demo`. Capture
starts only if explicitly enabled. It polls overlapping history plus the current
session, never sends an order, and never marks fees final. Set lookback to cover
the accounting period; older corrections require an explicit wider re-read or
broker import. Old local fills survive shorter subsequent queries.

`TANJA_ACCOUNTING_CONTRACT_SPECS` is a private JSON object keyed by **exact provider
instrument alias**. Example only — verify every instrument traded in your account:

```json
{
  "MNQ 12-26": {"contract":"MNQZ2026","multiplier":"2","tick_size":"0.25"},
  "NQ 12-26": {"contract":"NQZ2026","multiplier":"20","tick_size":"0.25"},
  "ES 12-26": {"contract":"ESZ2026","multiplier":"50","tick_size":"0.25"}
}
```

Aliases may be added for the same contract only with identical contract specs.
If the session endpoint uses an alias such as `MNQZ6` while history uses `MNQ 12-26`,
map both exact aliases explicitly to the same dated contract and specifications.
Current-session quantities, prices, sides and timestamps are compared with captured
history; a contradiction blocks readiness while delayed history catches up.
Account IDs, environments and specs bind separate ledgers; changing them requires
reconciliation of the new binding. A core-fill conflict persists as a blocker and
requires operator investigation; there is no automatic overwrite/reset switch.

Quote settings, after selecting a genuine data producer:

```text
TANJA_QUOTE_SOURCE=<producer identity>
TANJA_QUOTE_FEED_TOKEN=<separate random 32–128 character URL-safe secret>
TANJA_QUOTE_CONTRACTS={"ES":"ESZ2026","NQ":"NQZ2026","MNQ":"MNQZ2026"}
TANJA_EXECUTION_CONTRACT=MNQZ2026
```

Use actual current dated contracts, not these examples or continuous symbols.
The existing `TANJA_FEED_CONTRACT_MAP` must still establish alignment with analysis
candles. NQ and MNQ must have the same expiry; no copied prices or inferred basis.
The quote secret must differ from dashboard, candle-feed and broker-feedback
secrets. The quote token has no account-snapshot authority.

## Quote producer contract

POST envelopes to `/api/live-quotes` using the separate Bearer token:

```json
{"source":"producer-identity","generation":"unique-connection-id","sequence":1,
 "kind":"connected","sent_at":1791986400,"data":{}}
```

Next, consecutive sequences carry quotes for all three instruments:

```json
{"source":"producer-identity","generation":"unique-connection-id","sequence":2,
 "kind":"quote","sent_at":1791986400,
 "data":{"symbol":"MNQ","contract":"MNQZ2026","bid":23150,"ask":23150.25,
         "last":23150,"bid_at":1791986400,"ask_at":1791986400,"last_at":1791986400}}
```

All timestamps are UTC Unix seconds, including fractions if available. Examples
are historical schema illustrations and will correctly be rejected as stale.
End with `kind:disconnected,data:{}`. A new connection starts with a new generation
and sequence 1; old generations cannot return. Sequence here is the bridge's
consecutive delivery sequence. The producer must separately detect upstream
provider gaps and reconnect/reset after any gap. Coalesce upstream events before
numbering outgoing messages, preserve actual component clocks, and require an ACK
before advancing. The simple CLI stops on an uncertain response; restarting with
a new generation clears old prices.

Do not refresh last-trade timestamps with book events, midpoints, local heartbeat
times or transport send timestamps. For example, CrossTrade NT8 WebSocket frame
`epoch` is a frame send time, not documented exchange timestamps for all components.
That frame by itself cannot satisfy this contract.

Producer-side `tools/quote_bridge.py` uses private `TANJA_QUOTE_INGEST_URL` (HTTPS
ending exactly `/api/live-quotes`), the token and source above. Without `--run`
it checks configuration only. With `--run`, it reads quote **data objects** from
stdin, supplies envelopes and sends a disconnect on EOF. It never connects to a
paid source itself. It contains no retry queue that could replay stale prices.

## Offline tools and broker reconciliation

From `services/tanja`, with private variables loaded:

```sh
python3 tools/crosstrade_readonly.py check
python3 tools/crosstrade_readonly.py report
python3 tools/crosstrade_readonly.py capture-once
```

`capture-once` requires explicit capture enablement and uses only GET. `report`
prints private execution data: retain it outside Git and public logs. Reports
include a fingerprint of normalized local executions and latest captured costs.

To repair missed history, prepare a private normalized JSON file with two keys:
`fills` (CrossTrade-shaped rows, including actual account ID, environment, unique
executionId/fillId, instrument alias, side, quantity, price, UTC timestamp,
commission and fees) and `reconciliation`:

```json
{
 "account_id":"actual-account-id", "environment":"demo", "source":"broker_statement",
 "evidence_sha256":"SHA256-OF-THE-ACTUAL-BROKER-EXPORT",
 "flat_at":"2026-10-14T03:00:00Z", "through":"2026-10-14T20:00:00Z",
 "fingerprint":"SHA256-OF-NORMALIZED-COVERED-EXECUTIONS-AND-COSTS",
 "positions":{}, "fees_confirmed":["tv:demo:actual-fill-id"]
}
```

The operator must actually compare all fills, costs and inventory against the
broker evidence; hashing a file does not independently verify its contents. Use
the ledger's exact normalized report/fingerprint for the covered window after
importing any missing fills/costs. First run `import-fills --statement` with the
private normalized JSON; then `report --from-time <UTC> --through <UTC>` to obtain
the `window_fingerprint` for the intended window. That import records observations
only, without confirming coverage or fees. `fees_confirmed` must include every covered
execution exactly once, including opening fills for positions still open at the
cutoff. Positions are signed whole contracts by canonical dated symbol. Do not
claim a flat opening anchor if positions were carried in; obtain earlier history
back to a verified flat account anchor. Only a CLI reconciliation accepts proof,
not any public HTTP or CrossTrade capture endpoint.

```sh
python3 tools/crosstrade_readonly.py reconcile --statement /private/path/normalized.json --evidence /private/path/broker-export.csv
```

An import can preserve valid observed fills even if its reconciliation fails; it
never overwrites core fills. Fix the statement and retry. A changed covered cost
invalidates the seal. Equal-time fills in the same contract require a trustworthy
ordering source before the implementation can reconcile them; it does not guess
ordering from execution IDs.

## Verification

```sh
PYTHONPATH=services/tanja:services/tanja/vendor:services/tanja/tests python3 -m unittest discover -s services/tanja/tests -q
```

Tests include fee revisions across break-even, account scope, manual instruments,
partial aggregation, reversals, exact arithmetic, midnight attribution, capture
failures, cursor loops, stale quotes, reconnects and the rule that uncertain
accounting blocks entries while existing simulated stop management continues.

Primary interface references, checked 2026-10-15:

- https://crosstrade.io/docs/api/overview
- https://crosstrade.io/docs/api/tradovate/get-fills
- https://crosstrade.io/docs/api/tradovate/get-fill-history
- https://crosstrade.io/docs/api/websocket-api

The CrossTrade account response schemas and paid quote subscription still need
validation against the user's actual setup. There is no claimed live verification,
fee-finality guarantee or autonomous broker order loop in this release.
