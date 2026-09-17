# Downside Manager Shadow and Real Trade Replay deployment

## Upload to the existing GitHub repository

Extract `downside_manager_real_replay_dashboard_deploy.zip` over the **existing AgentSignals repository root**, preserving repository-relative paths. Commit and push the resulting files to the branch already deployed by the A/B Railway service, or run `railway up --service <existing-A-B-service-name>` from the complete repository. The ZIP is an overlay, not a standalone application.

For a repository that **already has shadow v1**, the functional update is:

| Repository path | Action | Purpose |
| --- | --- | --- |
| `downside_manager_shadow_v1.py` | Update | Causal decision trace and dashboard route registration |
| `dashboard.py` | Update | Four tabs under **Strategies → Downside Manager Shadow** |
| `downside_manager_dashboard.py` | Add | Terminal UI and read-only replay/live APIs |
| `real_trade_replay_v1/__init__.py` | Add | Replay package marker |
| `real_trade_replay_v1/catalog.json` | Add | Compact, frozen recent-trade candle paths and decision logs |
| `real_trade_replay_v1/build.py` | Add | Local reproducibility script; not run on Railway |
| `tests/test_real_trade_replay_dashboard.py` | Add | Focused local tests; not run on Railway |
| `DOWNSIDE_MANAGER_REAL_REPLAY_DEPLOY.md` | Add | This deployment guide |

The full ZIP also contains the previous shadow v1 runtime, frozen model, schema, and causal DOL/observer source files, so it can be applied over an existing AgentSignals checkout that has not yet received shadow v1. It excludes the full M1 archive, research outputs, and holdout. `catalog.json` contains only the short observed paths required to inspect the supplied sent trades; it is not an M1 data service.

## Routes

| Route | Function |
| --- | --- |
| `/downside-shadow` | Live shadow dashboard |
| `/downside-shadow/trades` | Filterable, sortable real-trade history |
| `/downside-shadow/real-replays` | Real replay history tab |
| `/downside-shadow/metrics` | Aggregate KPI and grouping by strategy, session, or month |
| `/downside-shadow/trade/<id>` | Clickable individual candle and decision replay |
| `/downside-shadow/api/trades` | Read-only compact replay list and metrics |
| `/downside-shadow/api/trade/<id>` | Read-only individual replay |
| `/downside-shadow/api/live` | Read-only live shadow list/status |
| `/downside-shadow/api/live/<id>` | Read-only live virtual path and decisions |
| `/downside-shadow/status` | Existing read-only shadow status |

The existing dashboard side navigation shows **Strategies → Downside Manager Shadow**, with **Live Shadow**, **Trade History**, **Real Trade Replays**, and **Metrics** tabs. All screens are served by the existing Flask/Railway app. There is no external dashboard, new service, worker process, or cron job.

## Configuration and database

No new environment variable or database migration is introduced by the replay dashboard. Keep `DATA_DIR` on the existing persistent volume. Set `DOWNSIDE_MANAGER_SHADOW_ENABLED=true` to run the live shadow; `false` keeps historical replay pages available while disabling live shadow ingestion. The existing v1 schema remains [downside_manager_shadow_v1_schema.sql](downside_manager_shadow_v1_schema.sql). Replay candles and decision logs live in the compact versioned JSON artifact, not in SQLite.

The replay builder uses the repository's `audit_results/handover_verification/source/trades.md`, `prices.csv`, and `audit_results/m15_context_20260912/causal_signals.jsonl` **locally only**. It is not part of Railway startup. Future new real trades require a deliberate catalog rebuild from their authoritative records; the live shadow page updates independently from `DATA_DIR`.

## Verification

```sh
python -m unittest tests/test_downside_manager_shadow_v1.py tests/test_real_trade_replay_dashboard.py -v
python -m py_compile agent.py dashboard.py downside_manager_shadow_v1.py downside_manager_dashboard.py
```

After GitHub/Railway deployment:

1. Open the existing app and choose **Strategies → Downside Manager Shadow → Real Trade Replays**.
2. The history must show 28 sent legs: 17 `REPLAYED`, 11 `UNREPLAYABLE` with specific reasons. Click a replayable row and inspect candles, markers, decision log, manager state, and actual/fixed/manager outcome cards.
3. Open **Metrics**. The sample label must read `SMALL FORWARD/RECENT SAMPLE — DESCRIPTIVE ONLY`; the aggregate is based on the 17 replayable legs.
4. Open **Live Shadow**. With the flag enabled, `/downside-shadow/api/live` reports enabled status and new eligible A/B emissions appear in the live table. Without the flag, historical replays still work.
5. No dashboard endpoint accepts a trading action or invokes a broker method.

The source table has two independently sent A/B legs at some timestamps; only `SENT` legs are candidates. No-fill and canceled rows are explicit `UNREPLAYABLE` records. All replayed fills are **modeled through-tick fills from M1**, not a claim that the broker filled at that exact tick. Broker dollars are displayed separately and are not compared as normalized R. The September 4 broker results differ sharply from the modeled fixed +2R outcomes, so the UI shows both without conflating them.
