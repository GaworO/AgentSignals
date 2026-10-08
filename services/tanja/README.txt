TANJA — RAILWAY OBSERVATION SERVICE v1

Start with SETUP.html for the step-by-step Railway and TradingView guide.

Scope implemented:
  - authenticated closed 1m bar feed; ES and MNQ stored separately;
  - durable SQLite archive and queue on a dedicated Railway /data volume;
  - per-market CSV downloads, duplicates/conflicts and arrival timestamps;
  - background v3 context packets with frozen inputs and no lookahead;
  - MNQ IFVG feature candidates, explicitly NOT approved trade signals;
  - dashboard: overview, candidates, AI context, 50K Builder executions, data;
  - optional read-only Tanja section in the main AgentSignals menu.

Not implemented in this service:
  - live AI calls (the independent research v3 runner remains separate);
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

Validation: 18 service tests pass, desktop/mobile UI and 5 main-menu sections pass.
Pine compiled in TradingView and added to ES chart; no alerts created.
No Railway deployment or real webhook delivery has been verified yet.
