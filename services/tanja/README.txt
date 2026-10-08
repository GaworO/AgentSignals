TANJA — RAILWAY OBSERVATION SERVICE v2.1 — AI OBSERVATION + NO-ORDER CONNECTION TEST

Existing users: start with API_SETUP.html for the API update.
New users: start with SETUP.html for the Railway and TradingView feeds.

Scope implemented:
  - OpenAI Responses API reviews, strict v3 schema and evidence validation;
  - frozen input/request/response audit, actual availability time and usage;
  - persistent daily attempt budget, cadence and no retry after errors;
  - authenticated closed 1m bar feed; ES and MNQ stored separately;
  - durable SQLite archive and queue on a dedicated Railway /data volume;
  - per-market CSV downloads, duplicates/conflicts and arrival timestamps;
  - background v3 context packets with frozen inputs and no lookahead;
  - MNQ IFVG feature candidates, explicitly NOT approved trade signals;
  - dashboard: overview, candidates, AI context, 50K Builder executions, data;
  - optional read-only Tanja section in the main AgentSignals menu.

Not implemented in this service:
  - autonomous full-strategy / 1:1 discretionary decisions;
  - broker connection, order submission, fills, P&L or account guard;
  - automatic history download/import, news feed or exchange calendar;
  - attribution of existing Agent 50k trades to Tanja.
No configuration flag or credentials can turn this build into a live executor.

Deploy only services/tanja as the Railway service root, one worker/replica.
Dockerfile starts gunicorn on PORT. Do not copy the other service's start command,
DATA_DIR volume, variables or TradersPost execution webhook.

Required env: DATA_DIR=/data, TANJA_FEED_TOKEN (random URL-safe 32+ chars),
TANJA_DASHBOARD_PASSWORD (separate random 16+ chars).
Optional env: TANJA_PARENT_ORIGIN (HTTPS origin of main dashboard),
TANJA_ES_TICKER=CME_MINI:ES1!, TANJA_MNQ_TICKER=CME_MINI:MNQ1!.
The ticker configuration is an exact allowlist; changing series requires a new
data directory/volume so different contracts are never silently merged.

Health: GET /health (no credentials, liveness only)
Feed: POST /feed/<TANJA_FEED_TOKEN> (private capability URL, don't share it)
Dashboard/API/CSV/Pine/guide: HTTP Basic username tanja, configured password.
Do not enable request access logging of the private feed URL.

Local verification (Python >=3.9):
  cd services/tanja
  python3 -m unittest discover -s tests -v

Storage and causal behavior:
  - first accepted bar is immutable; retries are idempotent, conflicts rejected;
  - only a minute received for BOTH markets queues a snapshot;
  - frozen_at is the receiver time when the pair was complete;
  - history queries require bar close <= cutoff AND received <= frozen_at;
  - pairs older than the last queued pair are archived, not evaluated retroactively;
  - worker restarts recover incomplete jobs; prior snapshots aren't rewritten;
  - latest 500 full snapshots retained; all bars, job metadata and candidates persist;
  - analysis reads at most 12,000 historical minutes per market; no ATH completeness claim;
  - gaps, including scheduled closures, are never filled;
  - 120s freshness threshold is not a market calendar; >180s late bars are rejected;
  - continuous contract/back-adjustment history and HTF anchor still need validation.

Two alerts stream forward only. They don't deliver the chart's historical bars.
Allow several complete 4h buckets to form; older history/news are still needed
for a full context evaluation. This service never treats warmup as permission to trade.

Source provenance: vendor/source_manifest.json records the research engine copied
from research_tanya_context_v3_20261008 without rule changes. Its research pivot,
FVG reset and HTF alignment conventions remain hypotheses, not verified trader rules.

AI operation:
  Optional by default. TANJA_AI_ENABLED=true requires OPENAI_API_KEY and
  OPENAI_MODEL. Defaults: 6 attempts / NY day, 15 minute spacing, 8000 output
  tokens, low reasoning effort. Fixed pilot window weekdays 09:30–11:00 NY.
  Minimum: 3 H4 + 3 H1 bars per market, latest shared cutoff <=120s old,
  15 contiguous recent minutes each. Older gaps and news remain unresolved.
  This is sampled context research, not per-candidate execution or 1:1 fidelity.
  Failure/invalid response/restart during a call pauses reviews durably until
  TANJA_AI_REVISION changes (or prompt/schema/model revision changes).
  Failures count; redeploying does not reset the daily limit.
  API key is never saved in requests/audits or included in frontend state.
  Store:false does not imply zero provider retention. Local audit is retained.
  API worker and bar-processing worker are separate; intake does no AI calls.
  Restart migration adds ai_runs and preserves existing bar/queue tables.

Read API_SETUP.html for exact variables, deployment and error recovery.
No real paid API call was made during delivery validation; tests mock the provider.
Validation results are in test-results.txt and ui-validation.json.
The user confirmed Railway feeds; this API update still needs upload/deployment
and the user's private API key. Main service integration is unchanged.

Step 3A connection test (EXECUTION_SETUP.html):
  TANJA_TRADERSPOST_TEST_WEBHOOK_URL: private dedicated Tanja webhook.
  TANJA_TEST_CONTRACT: explicit MNQ quarterly contract, e.g. MNQZ2026 (verify).
  POST /api/connection/test accepts only request_id, with HTTP Basic auth and
  X-Tanja-CSRF token from the authenticated /api/state. The server constructs
  a fixed test:true payload. It cannot send non-test orders, even if another
  environment variable claims to enable them. No call is triggered on startup,
  refresh, bar arrival or AI completion.
  At most five attempts / rolling 24h, at least 60s apart; no automatic retry.
  A receipt is not account verification or fill confirmation. No order, position,
  broker account read API or lifecycle manager has been added.
  Test UI requires modern secure browser context for crypto.randomUUID().
  User requested an actual Builder entry and exit. That remains a user-submitted
  trade after account-state review; it is not completed by this delivery.
  Testing used mocked transport only; no TradersPost request or order was sent.
