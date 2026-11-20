# Railway integration runbook — deployment authorized, memory repair verified

This release extends the existing `gunicorn agent:app` application. Do not deploy
`preview.py`, `web.py`, port 8876/8877 or the stale parent desktop source tree.
The existing `/ict-shadow`, automatic backup and trading processes stay intact.
The new extension has no broker transport and cannot enable live trading.

## Review and local validation

```sh
ICT_TEST_DATA_DIR=/absolute/path/to/research_tanja_month_202605_20261014 \
 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
 tests/test_ict_ai_trader.py tests/test_ict_native.py tests/test_ict_experiment.py services/tanja/ict_agent/tests \
 tests/test_ict_shadow_cloud.py tests/test_ict_shadow_dashboard.py \
 tests/test_ict_scanner.py tests/test_ict_history.py tests/test_directional_ai_transport.py
python3 -m services.tanja.ict_agent.preview --db /absolute/path/to/observer.sqlite3
```

The preview mounts the same navigation, Shadow and new tab handlers in a loopback
review harness. Existing trading workers are not started. Only that loopback
harness uses a fixture login; production routes always require existing dashboard
authentication (`AB_CONTEXT_SOURCE_PASSWORD`, fallback `TANJA_DASHBOARD_PASSWORD`).
Do not expose the preview outside loopback.

## Persistent files

Under the existing `DATA_DIR` (normally `/data/builder50`):

| Path | Purpose |
|---|---|
| `market_context/tanja.sqlite3` | Existing local intake; observer opens read-only |
| `ict_ai/observe.sqlite3` | New observations, events, hypotheses and optional LLM ledger |
| `ict_ai/replay.sqlite3` | Optional imported immutable replay journal |
| `ict_ai/paper-orders.sqlite3` | Separate explicit simulator ledger |
| `ict_ai/worker-status.json` | Freshness, mode and worker errors |
| `ict_ai/observer.lock` | One writer per shared volume |
| `ict_ai/knowledge/<release>/` | Private verified corpus/atlas/cue release |

A local filename is not accessible from Railway. Explicitly transfer the verified
private release and optional replay SQLite via an approved deployment workflow.
Do not commit transcript vaults, credentials, journals or 400 MB replay databases.
For any active SQLite copy use its online backup API; never copy a DB without its
uncheckpointed WAL or overwrite an existing live journal.

Current verified local release: `runtime-assets/knowledge/f674585237afa449a421` (136 text reviews / 2558 rules). The older pilot pins `f11b1442ab01a829038b` separately.
`assets.verify(path)` checks all manifest hashes; `Knowledge` additionally checks
SQLite/JSON agreement and source passage hashes. Build a new content-addressed
release for changed knowledge. Existing decision packets retain their own old
source records; never rewrite historical judgments to use newer knowledge.

## New settings — defaults preserve existing service behavior

- `ICT_AI_MODE=OFF` by default; authorized observer rollout may use `OBSERVE`.
- `ICT_AI_SOURCE_DB=/data/builder50/market_context/tanja.sqlite3` (default inferred
  from DATA_DIR), `ICT_AI_OBSERVE_DB=/data/builder50/ict_ai/observe.sqlite3`.
- `ICT_AI_CORPUS_DIR=/data/builder50/ict_ai/knowledge/<release>/corpus` and
  `ICT_AI_ATLAS_DIR=/data/builder50/ict_ai/knowledge/<release>/atlas`.
- Optional `ICT_AI_REPLAY_DB`, `ICT_AI_SHADOW_DB`, `ICT_AI_PAPER_DB` control **views**.
  Shadow defaults to the observer journal. Selecting a browser mode starts no worker.
- Optional `ICT_AI_PAPER_JOURNAL` selects the separate simulator journal.
- Existing auth is required even with missing data. The UI returns an explicit
  unavailable state instead of falling back to local research files.

SHADOW_AI additionally needs **all** of: `ICT_AI_PAID_APPROVED=true`,
`ICT_AI_APPROVAL_ID`, `ICT_AI_APPROVAL_EXPIRES_AT` (UTC epoch), `ICT_AI_PROVIDER=openai`,
`ICT_AI_MODEL`, `ICT_AI_API_KEY`, `ICT_AI_DAILY_CALLS`, `ICT_AI_DAILY_USD`,
`ICT_AI_INPUT_USD_PER_MILLION_CEILING`, `ICT_AI_OUTPUT_USD_PER_MILLION_CEILING`,
`ICT_AI_MAX_INPUT_TOKENS_UPPER`, `ICT_AI_MAX_OUTPUT_TOKENS`, `ICT_AI_TIMEOUT_SECONDS`.
No rates, model or trading policy are silently selected. Verify current rates
before approval. Token upper bounds use UTF-8 bytes plus schema/message cushion;
usage is accounted with configured ceilings, not represented as an invoice.
Unknown transport outcomes retain the full reservation and are never retried.
Over-bound usage halts subsequent calls pending reconciliation. Approval expires
independently of the API key. No browser endpoint can change these settings.

## Lifecycle and resources

The observer runs as an opt-in thread in the existing application, behind a
nonblocking file lock. Other Gunicorn processes can take the lock after the owner
exits. The source bridge catches up persisted closed minutes; only fresh snapshots
can trigger a model call. Schema/corpus mismatch blocks startup. A changed code or
knowledge manifest requires an explicit new observer run/journal rather than
mixing versions. No database is deleted or truncated automatically.

Local NumPy LSA holds dense TF-IDF vectors in memory (roughly 80 MB for that matrix,
plus arrays, source records and Python overhead). Startup verifies the corpus and
builds the index. Test actual Railway memory/CPU headroom before enabling OBSERVE;
no resource increase has been authorized. Journals grow with retained observations;
monitor capacity, do not delete market/trading evidence to free space.

`GET /ict-shadow/ai-trader/api/health` is authenticated and proves HTTP mount only.
`GET /ict-shadow/ai-trader/api/state?mode=observe` reports latest as-of, stale data and worker
state. Treat missing/stale worker status or source freshness as unhealthy. No live
risk gate is opened by a healthy observer. GET requests never start workers/calls.

## Before an explicitly approved deploy

1. Fetch `codex/directional-consolidated-20261026`; compare the actual Railway
   deployment SHA and working diff. Rebase/retest against intervening changes.
2. Verify the existing Dockerfile image on Python 3.12, dependencies and a cold
   start with ICT_AI_MODE=OFF. Then test OBSERVE with isolated data/volume before
   touching the live configuration. The local environment lacks Docker/Python 3.12.
3. Preserve trading flags, risk policies, active orders, AI ledgers and journals.
   Do not infer permission to alter them from approval to deploy this UI.
4. If a backup is needed, run `python backup_retention.py --root /data/builder50`
   in the **existing** container. Keep exactly one completed verified recovery
   archive. Do not add another scheduler or timestamped pre-backup.
5. Transfer only approved code and private assets. Verify release hashes and
   volume paths. Keep paid calls and the new worker OFF in the first UI rollout.
6. Check auth failures, both dashboards, exact source hashes, readable old journals,
   route compatibility and unchanged live flags. Paid advisory evaluation already has
   dated user authorization for a SHARED total of $8 before tax. Do not ask again.

Rollback should redeploy the previously verified code without deleting new or old
journals, or disable only ICT_AI_MODE. Do not restore a whole trading volume online;
any recovery restore requires offline reconciliation with broker state.

References: official Responses structured-output requirements and refusal handling:
https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses

## Native-tab release additions

Canonical UI is `/ict-shadow?view=ictai`, injected into the existing HTML shell;
no iframe/build service or new login. `ict_shadow_cloud.register` mounts the
protected extension. Existing cloud worker flags are unchanged. Legacy
`/ict-ai-trader` redirects within the same app, never to localhost.

`ICT_AI_REASONING_DB` optionally selects a separate read-only advisory ledger for
UI inspection; defaults to the selected market journal. Pilot results are in
`runtime-assets/experiments/<id>/reasoning.sqlite3` locally and must be explicitly
transferred to persistent storage for a later production viewer. Both new memory
tables are additive, created only by the authorized writer. Browser GETs never
run migrations. OFF mode creates no observer worker or paid ledger.

`.dockerignore` and `.railwayignore` exclude runtime-assets, local-runtime, SQLite
journals and environment secrets from code uploads. Required corpus assets must
be transferred separately and verified, never assumed present because local
preview works. The pinned first experiment and current canonical corpus have
different versions intentionally; choose a verified release before a later run.


## Authorized 20 November milestone and current blocker

Deployment and shared $8 API spending are explicitly authorized by the latest user prompt. Active production OOM kills were observed BEFORE this release was deployed. Do not push/restart or enable the worker until the memory failure and active/unknown AI calls are safely reconciled. See `NYAM_AI_ICT_2026-11-20.md`.

New lifetime budget settings: `ICT_AI_TOTAL_BUDGET_DB=/data/builder50/ict_ai/experiment-budget.sqlite3`, `ICT_AI_TOTAL_USD=8`. Every today/historical writer MUST use this SAME database and approval ID. Daily limits are additional restrictions. The stored approval/cap cannot be reset or raised. Orphan/unknown reservations remain charged. Set `ICT_AI_SESSION_DATE=2026-11-20`; observer reviews are restricted to 09:30–12:00 NY, at least 180 seconds apart, event-driven or a 900-second check. Historical same-session analysis is separately labelled replay.

An optional private `/data/builder50/ict_ai/runtime-config.json` supplies ONLY `ICT_AI_*` settings; it cannot contain `ICT_AI_API_KEY`. `ICT_AI_API_KEY_ENV=OPENAI_API_KEY` explicitly references the existing server secret without copying its value to the file. The latest user authorization permits its use for this separately accounted experiment. No configuration was installed yet. Never place credentials in reports, browser responses or code commits.

`/api/budget` and `/api/session-report` are authenticated read-only endpoints. A session report may be installed as `/data/builder50/ict_ai/session-report.json` with a factual `summary`; NOT_ANALYZED must never be relabelled NO_TRADE. The original frozen pilot must be regenerated as a new immutable plan after adding explicit `service_tier=default`, and its size must fit the budget left AFTER today's priority analysis.

Historical runner approval must bind `shared_budget_db` to the SAME lifetime ledger as today and `shared_total_usd=8`, using the SAME approval ID. Missing binding stops before accessing the provider. The plan-specific maximum remains an additional cap, never another $8 allowance.


## Memory repair and external replay

Hotfix 8aaf569 is deployed; measured stability is in reports/deployment-20261120/memory-stability.json. Native UI first deploys with ICT_AI_MODE=OFF. Heavy chronological preparation runs explicitly outside Gunicorn using `python -m services.tanja.ict_agent.session --source ... --target ... --release ... --date 2026-11-20 --frozen-at <available UTC epoch>`. It reads at most 512 minute groups per chunk, preserves original receipt times, reports gaps and never starts the legacy app or order transport. The dated separately approved reasoning runner uses the same lifetime budget ledger.
