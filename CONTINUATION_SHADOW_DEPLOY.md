# MNQ Continuation Policy-B shadow — deployment note

## 2026-09-22 directional repository update

This update adds the **separately identified exploratory SHORT research shadow**
`MNQ_CONTINUATION_HTF_CANONICAL_SHORT_RESEARCH_V1` alongside the existing frozen
LONG baseline. It does not change `MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1`,
its hashes, its LONG Development result, production Reversal, or Guard. SHORT
does not inherit any profitability claim from LONG.

Apply `MNQ_CONTINUATION_LONG_SHORT_COMPLETE_BUNDLE_V3.zip` at the application
root containing `agent.py`, preserving archive paths. It is a **complete
Continuation overlay**: it includes the original immutable LONG freeze,
isolated canonical detector dependencies, the exact `policy.py` and `rules.json`
research dependencies, supporting source/configuration files, plus the new
SHORT engine, integration and tests. It does not need the
earlier V2 or SHORT-only ZIP. Do not edit the freeze hashes. The existing
`requirements.txt` suffices; no new environment variable is required. Existing
`CONTINUATION_*` settings below continue to apply.

The live endpoint at `/continuation/api/status` reported
`RuntimeError: complete immutable Continuation freeze is unavailable` after a
SHORT-only deployment. That means the UI files reached the service but the
original freeze did not. Deploy this complete V3 overlay from the **application
root** rather than adding only the newer `continuation_*.py` files. The
dashboard now displays `engine.last_error` explicitly if the scanner fails.

SHORT uses the original Jade `SHORT` thesis, registered lower-side liquidity,
bearish canonical confirmation and causal OPEN bearish DOL. The shadow's
SQLite tables gain a `direction` column; existing rows default to LONG.
Dashboard and candidate cards identify the direction, with a direction filter
on the candidate page. The shadow remains broker-inert. Software tests and the
real `seed.csv` scanner smoke test do not evaluate SHORT profitability.

To verify after overlay:

```sh
python -m unittest discover -s tests -p 'test_continuation*.py' -v
python continuation_scan_runtime.py --import-check
python -c 'import continuation_shadow; print(continuation_shadow._verify_freeze())'
```

Then open `/continuation/candidates` and `/continuation/dashboard`. The SHORT
source lock checks the new engine's exact hash; the existing full LONG freeze
and canonical source hashes remain checked before every scan.

Identity: `MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1`  
Mode: forward shadow only; no broker, webhook, Guard, partial, BE, trailing, or production-Reversal mutation.

## Changed files

- `continuation_shadow.py` — independent scanner, SQLite state, simulated orders/trades, dashboard and candidates routes.
- `continuation_scan_runtime.py` — runs the frozen scanner in its own Python process.
- `continuation_runtime/detcore/` — unmodified research detector dependencies, isolated from the production Reversal detector.
- `agent.py` — sends each already-persisted closed bar to the independent shadow worker and registers its routes.
- `dashboard.py` — adds the visible **Continuation** strategy item.
- `tests/test_continuation_shadow.py` — freeze, accounting, warm-up, restart/dedup and broker-inert checks.
- `MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE/` — complete original immutable package: detector source, configuration, SHA manifests, candidate/order/trigger manifests and audit files. Hashes are unchanged.
- `MNQ_JADECAP_HTF_PULLBACK_V1_OUTCOME_FREE_FREEZE/source/mini_v1_authoritative.py` — exact causal JadeCap thesis source.
- `J2_LONG_BREAKOUT_PULLBACK_MINITEST_20260922/run_outcome_free.py` — frozen registered-liquidity/DOL catalog helpers used by the baseline.
- `audit_results/ict_125/year_20260910/registration.json` — selected generic configuration, SHA-256 `959f2ab4…`.
- `MNQ_CONTINUATION_HTF_CANONICAL_REVERSAL_SOURCE_GATE/SOURCE_GATE.json` — original source gate referenced by the freeze.

## Deploy

1. Extract the ZIP into the application root (the directory containing `agent.py`), preserving its paths. It adds the missing frozen directories and replaces only the listed integration files.
2. Keep the existing `audit_results/ict_125/policy.py` present. The package includes the research `detcore` under `continuation_runtime/`, so it never replaces the production Reversal modules. The full freeze and source hashes are checked before every scan; a missing or modified member fails closed.
3. Deploy normally with the existing `requirements.txt`; no new dependency is required.
4. Use a persistent `DATA_DIR`. The shadow database is `${DATA_DIR}/continuation_shadow.sqlite3` and the scanner reads `${DATA_DIR}/archive.csv`, falling back to `${DATA_DIR}/buffer.csv` during the first startup.
5. Open `/` and choose **Continuation**. `/continuation` and `/continuation/candidates` show the Reversal-style six-step candidate funnel; `/continuation/dashboard` shows live metrics.

Optional environment:

- `CONTINUATION_SHADOW_ENABLED=0` disables the worker without removing the read-only routes.
- `CONTINUATION_DB=/persistent/path/file.sqlite3` overrides the owned database.
- `CONTINUATION_HISTORY_CSV=/persistent/path/archive.csv` overrides market history.
- `CONTINUATION_INSTRUMENT_ID=1` is used only when history does not contain physical `instrument_id`.
- `CONTINUATION_MAX_HISTORY_DAYS=0` keeps the exact default: no time truncation. A positive value changes the input history and is not parity mode.

## Verified

- Immutable freeze root, every member hash, and all runtime source hashes are checked before each scan. The large Development market CSV named in `SOURCE_HASHES.json` is not a runtime dependency and is not distributed.
- The clean base project plus this complete V3 ZIP imported both engines through `continuation_scan_runtime.py --import-check`, passed `_verify_freeze()`, and passed all 11 Continuation tests with installed `requirements.txt` dependencies.
- A real scan of the clean project's bundled `seed.csv` (100,000 MNQ rows) completed: 233 canonical LONG outputs and 202 canonical SHORT outputs; SHORT had 23 eligible orders and four estimated fills. These are scanner diagnostics, not a profitability replay.
- Flask returned HTTP 200 for `/`, `/continuation`, `/continuation/candidates`, `/continuation/dashboard`, both list/status APIs and a real candidate detail API. Home navigation contained both DOL Delivery Reversal and Continuation.
- The original freeze manifests contain 334 Development orders and 126 estimated fills; they were retained unchanged. No Validation or SEALED data was loaded.
- Warm-up records historical candidates but arms strictly after the last warm-up bar, so it cannot create fake forward trades.
- Policy B uses the frozen pre-entry OPEN DOL, variable realized R, and `$3.50 / (risk points × $2.00)` once per completed MNQ trade.
- Fill-bar brackets are disabled; later ambiguous bars are adverse/SL-first; stop gaps execute at the adverse open.
- SQLite uniqueness makes candidate, order and trade creation idempotent across restarts.
- Existing DOL Delivery Reversal, DOL Reversal Manager, A Continuation, management and archived-research navigation/routes remain unchanged; Continuation is added alongside them.

## Known limitation

The current live `agent.py` archive schema does not retain Databento physical contract IDs. Therefore exact contract-roll termination is available when the configured history file contains `instrument_id`; otherwise the dashboard explicitly reports that physical contract IDs are unavailable and the live epoch remains open across the continuous-symbol roll. No roll is guessed from price or calendar. This does not affect the verified 126-trade Development parity, whose source contains physical IDs.

The shadow recomputes the canonical detector from persisted causal history in a coalesced background worker. It never blocks `/bars`, but very large untruncated archives can make dashboard state arrive several bars late. The worker catches up from persisted bars and does not fabricate intrabar ordering.
