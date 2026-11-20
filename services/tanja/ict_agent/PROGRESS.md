# ICT observer implementation — 20 November 2026

Scope: a local, persistent extension to Tanja, using the existing ICT corpus and
real ES/NQ/MNQ history. Live execution is absent from this extension. Existing
service configuration, databases, broker adapters and deployment are unchanged.

## Audit

- `../store.py`, `../worker.py`: authenticated closed-minute intake, receipt-time
  filtering, durable queue and context generation already exist.
- `../vendor/context_layer.py`, `context_geometry.py`, `tanya_replay.py`: causal
  aggregate, range, FVG and SMT primitives. Reuse their documented conventions;
  the new incremental implementation will be compared against these primitives.
- `../ai_review.py`: Responses API, strict schema, bounded attempts and audit.
  Its current prompt/library is not the new 615-video corpus. The extension adds
  a corpus-grounded review boundary without enabling the old execution path.
- `../paper_lifecycle.py`, `../execution_ledger.py`, `../broker_feedback.py`,
  `../limited_live.py`: existing quote simulation and guarded broker components.
  Do not import/start the live application as a shortcut to an offline test.
- `research/ict_model_tests_20261119/engine.py`: retain the frozen 2022/IFVG
  detectors as comparison baselines, not the entire scenario engine.
- `research/ict_entry_reference_20261120`: lossless structured atlas, 46 curated
  families and 85 directional guides. None is certified executable.
- `research/ict_knowledge_20261119/corpus_615`: 615 catalog entries; coverage
  currently reports 134 full *available-text* reviews, not 615 complete videos.
  Source/cue hashes, missing intervals and actual counts will be rechecked.
- `research_tanja_month_202605_20261014/{NQ,ES,MNQ}.csv`: real archived OHLC,
  previously used for development. Not an untouched holdout. No measured receipt
  timestamps, volume, bid/ask or verified numeric-ID-to-dated-contract mapping.

## Architecture / acceptance

One package under the existing service: normalization → incremental observations
→ durable context and hypotheses → hybrid corpus retrieval → deterministic
decision + optional validated AI review → read-only dashboard → paper candidate
and independently gated simulation. SQLite commits a complete observation and
its state atomically. Source corpus is read-only. The Tanja bridge reads existing
intake tables without starting workers or changing trading settings.

Semantic retrieval will be explicitly labelled with its actual algorithm.
Source provenance verifies extraction, not profitability or visual accuracy.
Unknown parameters and conflicting variants remain unresolved.

## Progress

- Implemented the integrated observer, corpus audit/RAG, state journal, hypotheses,
  risk boundary, broker protocol simulator, read-only bridge, API adapter and UI.
- 22 new unit tests + 3 delivery checks + 7 existing NQ intake tests + 4 existing
  risk sizing tests passed (36 total). Provider responses in tests are fixtures.
- 3,799 full-review source references checked against original cue hashes.
- Real one-session smoke replay and full May final replay completed in
  `research/ict_agent_validation_20261120/runs/71e60464703a3716`.
- UI checked at default size and 360px: six chart panels, symbol switching,
  chronological candidate selection and range control work; no horizontal
  overflow or JavaScript errors observed.
- Process environment has no OPENAI_API_KEY or OPENAI_MODEL. A local Tanja seed
  database was inspected read-only; its latest ES/NQ/MNQ bars end 6 November,
  so it is not a verified current live feed.
- No paid model request, live connection, deployment or order sent.

## External evidence / design constraints

- Responses strict JSON output: https://developers.openai.com/api/docs/guides/structured-outputs
- CME contract multipliers/ticks: https://www.cmegroup.com/articles/faqs/micro-e-mini-equity-index-futures-frequently-asked-questions.html
- Pine supported request sources: https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/
- TradingView outgoing alerts: https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/

Pine's documented request family does not provide arbitrary backend HTTP reads.
Reuse the existing outgoing feed; put persistent decision history and backend
annotations in our dashboard. The UI must not label deterministic commentary
as a completed LLM analysis.

## Final validation

Run `71e60464703a3716` completed on real archived ES/NQ/MNQ data. The data/code
manifest and copied baseline are frozen; source code remained unchanged during
the run. No production setting was changed.

- 34,260 chronological minute batches, including warmup.
- 1,977 immutable decision snapshots; 2,533 legal lifecycle transitions.
- 643,447 event-anchor checks and 942,173 snapshot-anchor checks passed.
- 73 exploratory candidates: at the primary 1-second latency assumption,
  19 cancelled, 37 execution-ambiguous, 17 conditionally closed in the OHLC model.
  These are not actual broker fills or net profitability results.
- Existing frozen baseline: 0 complete 2022 candidates and 1 IFVG candidate;
  that candidate's execution is ambiguous under the same primary assumptions.
- Measured local processing median 18.2 ms, p95 35.8 ms. Feed, model and broker
  latency were not measured.
- Zero paid model calls and zero actual orders. The risk policy is unconfigured
  by default; every proposed plan remains blocked from execution.
- Development/validation/retrospective-test and walk-forward date partitions
  are saved, but no previously untouched holdout is claimed.

See `research/ict_agent_validation_20261120/latest.json`, `test-summary.json`,
`delivery-tests.json`, `ui-validation.json`, `knowledge-audit.json`,
`knowledge-completeness-detail.json`, `connection-audit.json` and the run's
`evaluation.json`, `split-plan.json`, `example-market-packet.json` and
`example-api-request.json`.

The prototype milestone is complete. Autonomous source-faithful selection,
actual model evaluation, current verified live feed, calibrated execution
performance and live-trading readiness remain unproven. Exact remaining gates
and component boundaries are listed in README.md; live execution is absent.

## Agent50k integration — 20 November, next milestone

This port now mounts through root `ict_ai_trader.py` in the existing Agent50k
application. `integrated_web.py` serves the authenticated same-origin UI;
`runtime.py` reads its existing local intake; `reasoning.py` reuses the existing
provider transport with independent authorization and budget accounting. See
root STATUS.md for current counts, source expansion and unresolved gates.
The original May run was not rewritten or relabelled as LLM reasoning.


## Native ICT Shadow and genuine reasoning milestone — 20 November 2026

- Replaced separate iframe view with native `/ict-shadow?view=ictai`; preserved
  three existing tabs/auth and review links. Legacy UI bookmark redirects internally.
- Added structured entry/lifecycle/changes, persistent validated hypothesis ledger,
  context-sensitive bidirectional/exception retrieval, five actual model states.
- Froze 15 historical cases / 30 B/C requests plus A baseline. Estimated $2.31;
  hard proposed cap $8. Approval false, zero calls. Blind exports and rating scorer.
- Read all 56 cues of Failed OTE/FOMC, added 7 rules, preserved prior 2,551 hashes.
  Canonical 136/615, 2,558 rules, 3,824 refs. Atlas 95 checks PASS. The experiment
  retains the earlier pinned 135-video release for reproducibility.
- 150 tests PASS in 91.13s. Native tab verified with real historical candidate,
  six charts and no iframe. No production deployment or live orders.
- All 37 previously audited fills remain ambiguous. No new tick/quote evidence.
- Pending: independent source/setup adjudication, authorized genuine model run,
  forward paper, remaining corpus, current deployment SHA and Python 3.12 image.


## 20 November execution preflight
User explicitly approved deployment and one shared $8 API experiment. Production preflight found a new OOM kill (23 to 24) and Gunicorn SIGKILL/restarts; no deployment, flag changes or new calls were made. Added shared immutable lifetime budget, real-transport model/rate guard, explicit standard tier, dated NYAM event pacing and authenticated usage/report UI. Regression suite: 154 PASS; additional final guards: 5 PASS; protected endpoint check: 1 PASS. See root NYAM_AI_ICT_2026-11-20.md for actual data coverage and blockers.
