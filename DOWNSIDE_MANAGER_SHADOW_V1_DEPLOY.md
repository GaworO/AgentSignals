# Downside manager shadow v1 deployment

This package overlays the existing AgentSignals repository and its existing A/B Railway service. It does not replace that repository or the service's current start command. The shadow never submits or modifies an order.

## Files

| Repository path | Purpose | Deploy? |
| --- | --- | --- |
| `agent.py` | Observe eligible canonical emissions after persistence; queue bar updates; register routes | Yes |
| `dashboard.py` | Add shadow navigation | Yes |
| `downside_manager_shadow_v1.py` | Frozen two-path shadow and status page | Yes |
| `downside_manager_shadow_v1_schema.sql` | Separate SQLite persistence schema | Yes |
| `requirements.txt` | Add Pillow for the frozen DOL audit module import | Yes |
| `rl_trade_manager/downside_manager_v1/model.json` | Frozen classifier coefficients | Yes |
| `rl_trade_manager/downside_manager_v1/threshold.json` | Frozen threshold provenance | Yes |
| `rl_trade_manager/{__init__,env,features,m1_state,types,policies,trade_dataset}.py` | Causal observer and execution engine | Yes if absent from deployed checkout; included in archive |
| `ab_dol_live.py`, `audit_results/ict_125/{policy.py,rules.json}`, `audit_results/A_CONT_V3_ICT_NARRATIVE/representation_audit_20260914/audit.py`, `audit_results/ab_dol_state_audit_20260915/run.py`, `audit_results/ab_dol_qualitative_random10_20260915/build_two_horizon_preoutcome.py`, `detcore/a_cont_v3_ict_{dol,ledger,set}.py` | Existing causal DOL runtime | Yes if absent from deployed checkout; included in archive |
| `tests/test_downside_manager_shadow_v1.py` | Local focused checks; historical parity check needs local TRAIN artifacts | No |

No datasets, historical results, holdout files, or large replay artifacts are in the archive. Existing files not in the archive remain necessary for the normal AgentSignals app.

## Environment

| Variable | Required? | Meaning |
| --- | --- | --- |
| `DOWNSIDE_MANAGER_SHADOW_ENABLED` | New; set `true` to run; defaults to `false` | Shadow feature flag; `false` is the rollback switch. |
| `DATA_DIR` | Existing; required for restart-safe persistence | Directory already used by the A/B app for `buffer.csv` and `journal.db`; set to the mounted Railway volume path, typically `/data`. The shadow creates `downside_manager_shadow_v1.sqlite3` there. |
| `ACCOUNT`, `RISK_PCT`, `POINT_VALUE` | Existing sizing settings | Used only when the canonical emitted record has no explicit sent quantity; retain existing values. |

The only dependency change is `Pillow`, required by the frozen DOL audit module's import. The shadow needs no separate worker, cron, or Railway service. It starts in the existing `agent:app` process when the flag is true and uses a daemon thread that catches up from the already persisted M1 buffer. Keep the existing Railway start command.

## Deploy

1. Extract `downside_manager_shadow_v1_deploy.zip` over the matching existing AgentSignals repository root, preserving directories. Review the listed overlay files before upload.
2. Confirm the A/B service still mounts its existing persistent `DATA_DIR` volume. Keep existing strategy/broker variables unchanged.
3. From that complete repository, run `python -m unittest tests/test_downside_manager_shadow_v1.py -v` where TRAIN artifacts are present. The SQLite schema is created automatically on service startup when enabled. For a local schema check: `python -m downside_manager_shadow_v1 migrate` (this writes to local `DATA_DIR`, not the Railway volume).
4. Link the existing Railway project if needed with `railway link`. Upload to the existing A/B service with `railway up --service <existing-A-B-service-name>`. This command deploys the complete repository from the current directory; do not deploy the small overlay ZIP as a standalone service.
5. In that existing Railway service's **Variables** tab, set `DOWNSIDE_MANAGER_SHADOW_ENABLED=true`, review/apply the staged change, and redeploy if prompted. No broker variable or start command change is needed.

## Migration and persistence

On enabled startup, `downside_manager_shadow_v1.register(app)` executes the included schema with `CREATE TABLE IF NOT EXISTS` and indexes. No manual production migration command is needed. The separate database contains:

- `downside_shadow_trades`: identity, signal/entry/quantity/risk, each virtual path's status/stop/final R/exit reason, causal state snapshots, probability/recommendation, model/threshold/policy/feature/DOL hashes.
- `downside_shadow_decisions`: time-stamped recommendation and two-path status/state snapshots.
- `UNIQUE(strategy_id, source_key)` and `UNIQUE(trade_id)` prevent a duplicate shadow trade after restart or repeat signal; `UNIQUE(trade_fk, decision_ms)` prevents a duplicate decision. Status/time and strategy/fill-time indexes support refresh and dashboard reads.

Exact columns in `downside_shadow_trades`: `id`, `source_key`, `trade_id`, `strategy_id`, `signal_json`, `direction`, `entry`, `initial_sl`, `fixed_tp`, `quantity`, `initial_risk`, `signal_ms`, `entry_anchor_ms`, `fill_ms`, `status`, `control_status`, `manager_status`, `last_bar_ms`, `pre_bars_json`, `bars_json`, `dol_states_json`, `state_quality`, `control_virtual_sl`, `manager_virtual_sl`, `current_r`, `mfe_r`, `mae_r`, `dol_state_json`, `m1_state_json`, `manager_probability`, `recommendation`, `control_final_r`, `manager_final_r`, `delta_r`, `control_exit_reason`, `manager_exit_reason`, `model_hash`, `threshold`, `policy_version`, `feature_schema_version`, `dol_runtime_hash`, `created_at`, `updated_at`.

Exact columns in `downside_shadow_decisions`: `id`, `trade_fk`, `decision_ms`, `control_status`, `manager_status`, `current_r`, `mfe_r`, `mae_r`, `control_virtual_sl`, `manager_virtual_sl`, `manager_probability`, `recommendation`, `dol_state_json`, `m1_state_json`, `created_at`. Explicit indexes: `idx_downside_shadow_status(status,last_bar_ms)`, `idx_downside_shadow_strategy(strategy_id,fill_ms)`, `idx_downside_shadow_decision_time(decision_ms)`.

The shadow tracks theoretical through-fills from closed M1 bars and does not claim broker fill reconciliation. It only observes A/B emissions accepted by the existing canonical path. A Continuation BOTH_ALIGNED is a second virtual label for its eligible A/B parent, not an independently sized extra position. Strategy C remains unconnected because it runs in another service and lacks this frozen observer.

## Verify

1. Railway logs show `[downside-shadow] ENABLED SHADOW ONLY model=3add0173ba3e threshold=0.921934593 db=...` and no `refresh error` or `DOL engine unavailable` line.
2. `GET /downside-shadow/status` returns `enabled:true`, `shadow_only:true`, `broker_execution:false`; `GET /downside-shadow` and the dashboard navigation show `SHADOW ONLY — NO BROKER EXECUTION`.
3. After the first eligible emitted A/B signal, `total` rises by one (or two if the same signal also meets A_CONT_BOTH_ALIGNED); the new SQLite database has a `PENDING` row. The first modeled through-fill moves it to `OPEN`; completed paths move it to `DONE` with both final R values and delta.
4. On an open shadow trade, check the recommendation and probability, control and manager virtual stops, M1/DOL state, and current R. A duplicate intake must leave the same source key with one row per strategy.
5. The shadow module imports no broker client, order emitter, guardrail adapter, or HTTP client. Canonical broker activity comes only from the pre-existing app path.

## Rollback

Set `DOWNSIDE_MANAGER_SHADOW_ENABLED=false` in the existing Railway service's Variables tab and apply/redeploy that variable change. Canonical trading continues. The separate shadow SQLite file is retained for audit and is not read when disabled.

## Exact archive manifest

All paths below are relative to the existing AgentSignals repository root.

| File | Purpose | Must deploy? |
| --- | --- | --- |
| `agent.py` | Application integration/dashboard | Yes |
| `dashboard.py` | Application integration/dashboard | Yes |
| `downside_manager_shadow_v1.py` | Frozen observer/DOL runtime support | Yes |
| `downside_manager_shadow_v1_schema.sql` | SQLite migration | Yes |
| `DOWNSIDE_MANAGER_SHADOW_V1_DEPLOY.md` | Deployment instructions | No |
| `requirements.txt` | Dependency manifest | Yes |
| `tests/test_downside_manager_shadow_v1.py` | Local verification | No |
| `rl_trade_manager/__init__.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/env.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/features.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/m1_state.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/types.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/policies.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/trade_dataset.py` | Frozen observer/DOL runtime support | Yes |
| `rl_trade_manager/downside_manager_v1/model.json` | Frozen model/threshold | Yes |
| `rl_trade_manager/downside_manager_v1/threshold.json` | Frozen model/threshold | Yes |
| `ab_dol_live.py` | Frozen observer/DOL runtime support | Yes |
| `audit_results/ict_125/policy.py` | Frozen observer/DOL runtime support | Yes |
| `audit_results/ict_125/rules.json` | Frozen observer/DOL runtime support | Yes |
| `audit_results/A_CONT_V3_ICT_NARRATIVE/representation_audit_20260914/audit.py` | Frozen observer/DOL runtime support | Yes |
| `audit_results/ab_dol_state_audit_20260915/run.py` | Frozen observer/DOL runtime support | Yes |
| `audit_results/ab_dol_qualitative_random10_20260915/build_two_horizon_preoutcome.py` | Frozen observer/DOL runtime support | Yes |
| `detcore/a_cont_v3_ict_dol.py` | Frozen observer/DOL runtime support | Yes |
| `detcore/a_cont_v3_ict_ledger.py` | Frozen observer/DOL runtime support | Yes |
| `detcore/a_cont_v3_ict_set.py` | Frozen observer/DOL runtime support | Yes |
