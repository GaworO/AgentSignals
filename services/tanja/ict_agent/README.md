# Agent50k integration

This package is now mounted by the existing `agent:app` at `/ict-ai-trader`.
Read [project status](../../../STATUS.md) and
[deployment instructions](../../../docs/ict-ai-trader/DEPLOYMENT.md).
The following section describes the original replay prototype and remains useful
for its frozen results; its old local-only limitations are superseded only where
root STATUS.md explicitly records the new implemented capability.

# Tanja ICT observer

Persistent, source-grounded, **read-only** extension to the existing Tanja service.
This is a working development prototype, not a profitable autonomous trader.
The canonical corpus and existing production service remain separate from its
derived observation journal. There is no live-order transport in this package.

## Run locally

From the repository root, Python 3.11 with `numpy`, `flask`, `jsonschema`:

```sh
python3 -m services.tanja.ict_agent replay --start 2026-05-01 --end 2026-05-31
python3 -m services.tanja.ict_agent serve --port 8876
python3 -m unittest discover -s services/tanja/ict_agent/tests -v
python3 research/ict_agent_validation_20261120/check_delivery.py
```

Open http://127.0.0.1:8876. The server binds only loopback. It is a development
viewer, not a production deployment. Every HTTP route is read-only. SQLite is
read in `mode=ro` by the UI. The first-candidate button selects the first
chronological exploratory proposal, not a retrospectively profitable example.

The replay consumes the existing independently priced `NQ.csv`, `ES.csv` and
`MNQ.csv` in `research_tanja_month_202605_20261014`. It records raw numeric
instrument IDs, never silently substituting NQ prices for MNQ. Availability is
assumed at minute close because receipt times are absent from this export.
Four calendar days before the selected interval are used for warmup. Missing
minutes are never synthesized. The bounded May evaluation excludes May 25;
this is not a complete exchange holiday calendar.

Each run is content-addressed under `research/ict_agent_validation_20261120/runs`.
Its manifest contains input hashes, corpus/atlas dependencies, code hash and
the explicit research policy. Code and the existing baseline engine are frozen
with it. A rerun uses the same immutable batches; conflicting duplicates fail.
`latest.json` is published only after replay and journal validation finish.

## Read the existing live feed without changing it

```sh
python3 -m services.tanja.ict_agent observe-tanja \
  --source /absolute/path/to/tanja.sqlite3 \
  --db /absolute/path/to/new-ict-observer.sqlite3 --follow
```

Supply a local, accessible Tanja intake database. The bridge reads `bars` and
`collection_bars` in SQLite read-only mode, preserves receiver times and exact
ticker bindings, requires shared minute windows, and reports missing pairs.
It neither imports the live app nor starts its execution workers. A continuous
`NQ1!` ticker remains explicitly unverified for dated-contract execution.
No scheduler, Railway deployment or service configuration was installed.

## Components and boundaries

| Component | Working implementation | Remaining boundary |
|---|---|---|
| A / F Knowledge + RAG | `knowledge.py`: all 2,533 canonical records, verified cue hashes, curated atlas, TF-IDF + 64D local LSA, direction filters, cases and exception lanes | LSA retrieval quality not benchmarked; no pretrained multilingual embedding claim; incomplete corpus |
| B Data | `market.py`, `bridge.py`: NQ/ES/MNQ separation, closed candles, receipt timestamps, explicit contract reset, NY/DST, missing intervals | Historical feed latency, dated contract definitions/roll proof, ticks/quotes, full exchange calendar |
| C Events | Incremental pivots, equal extremes, sweeps, close breaks, displacement, FVG/IFVG, complete prior session/RTH levels | OB/breaker selector, verified discretionary range selection, previous-week completeness, news and calibrated regime model |
| D Memory | `store.py`, `observer.py`: atomic batches, durable market state, immutable snapshots, event journal and dedupe | Production load/retention/HA not validated |
| E Hypotheses | `scenarios.py`: reversal, continuation, failed reversal and no-trade for each opportunity; supporting/opposing evidence and source retrieval per hypothesis | No claim to autonomous trader-quality ranking; one explicit exploratory sequence generates price proposals |
| G AI | `ai.py`: source-grounded packet, strict Responses JSON schema, reference/time checks, attempt reservation, no automatic retries | No paid provider calls or calibrated semantic evaluation performed. Commentary is deterministic until an actual review is saved |
| H Lifecycle | Stable opportunity IDs, waiting/ready/invalidation/expiry, legal transition graph including order/position states | Observer journal is not a live broker position ledger |
| I Risk / execution | `risk.py`: costs, units, limits, quote/account freshness, mapping, news, session and reconciliation gates. `simulator.py`: durable protocol simulator, partial fills, dedupe, stops, restart/disconnect reconciliation | No live gateway. Quote/account fixtures are used only in tests; OHLC replay cannot assert real fills |
| J Dashboard | `web.py`, `static/`: exact as-of timeline, six timeframes, range/level provenance, hypotheses, exceptions and source links | No live deployment or authenticated remote endpoint |
| K Journal | Bars, frames, events, immutable decisions, setup transitions, source retrievals and separate AI audit in SQLite | Full broker fees/fills/accounting remain in existing Tanja modules, not overwritten |
| L Evaluation | `replay.py`: real chronological replay, frozen original 2022/IFVG baseline, abstain baseline, conditional OHLC outcome envelopes and latency; chronological/walk-forward partitions | No untouched holdout, actual execution metrics or proven edge |

## Knowledge audit

The source catalogue has **615 unique videos**. Current canonical coverage:
134 full available-text reviews, 339 pending, 136 missing sources, 2 partial
reviews and 4 quarantined sources. A full text review does not verify audio or
chart interpretation. All 3,799 full-review fragment references were rehashed
against original cue storage; canonical SQLite and JSON rule contents agree.

The 2,533 records contain 2,417 full-text-review records and 116 legacy partial
records. Extraction labels: 2,411 explicit, 4 inferred, 118 unknown. There are
1,457 exception entries in 1,034 records, 40 records with stored contradictions,
2,310 with ambiguities and 140 whole-video audit notes. The 366 case candidates
are identified from stored labels/text and structured without inventing outcomes;
they are not 366 independently verified complete trades. Cross-video conflict
review remains incomplete. None of these counts establishes profitability.

`knowledge-audit.json` includes each video's unreviewed **cue-index intervals**
(exclusive ends), missing-source state and source-quality caveats. No available
cues for a missing video means unknown coverage, not zero remaining work.
Catalogue 451 remains source-missing. Catalogue 463 has a full available-text
review with unverified media completeness; its earlier caption caveats remain.
A total of 178 videos have any rule reference: 134 full available-text reviews
and 44 additional videos with legacy excerpts. See
`knowledge-completeness-detail.json` for those exact videos and excerpt timestamps.
Uncovered cue intervals mean no verified full-cue audit; they do not erase retained
legacy fragment work.

Newly downloaded playlists outside canonical integration are not promoted to
analyzed knowledge by this service.

## Decisions and chart semantics

The observer detects events on each closed minute. It saves a detailed decision
on meaningful events/lifecycle changes or five-minute reassessment. No LLM runs
per tick. Every historical chart query is limited to the selected snapshot's
`as_of` and exact contract. Confirmed and provisional aggregates are separated.
Pivots require a closed right-hand candle. A stop-location interpretation is a
price-derived hypothesis, never measured resting liquidity.

Ranges are explicitly labelled **trailing 20 completed bars** (or fewer during
warmup), not asserted to be the trader's chosen dealing range. D1 is the
complete 18:00–17:00 NY electronic session; H1/H4 start from the 18:00 NY anchor.
Gap-containing windows are labelled. ES/NQ SMT geometry compares the same
complete current and previous RTH windows; anchor choice remains a research
convention. Correlated model/FVG/time-window labels share one opportunity ID.

`READY` in an exploratory setup means its **research price plan** is geometrically
complete. The decision remains a `TRADE_CANDIDATE`, and the independent risk
gate refuses it because no risk policy/account/quote authorization is configured.
It does not mean live-ready. The proposed stop/target and numerical displacement
thresholds are versioned research choices, not a splice of incompatible films.

## API use

The same local state is available via:

- `GET /api/state`, `/api/timeline?before=<epoch>`
- `GET /api/snapshots/<id>` and `/api/snapshots/<id>/charts?symbol=NQ&limit=180`
- `GET /api/retrievals/<id>` and `/api/evaluation`

`ai.packet(store, snapshot)` expands source records, audit notes and all causal
market anchors. `ai.request(packet, explicit_model)` prepares a Responses request
with strict `text.format`. The example request is **not sent** and contains an
explicit model placeholder. `review_once` has no ambient credentials or default
transport. An authorized caller can inject the existing Tanja
`ai_review.call_openai(payload, key)` with its own billing policy. A persistent
attempt cap and one reservation per snapshot prevent blind retries. Unknown
transport outcomes remain charged-attempt candidates, not safe-to-repeat calls.

The current review runner is for fresh read-only observations; old replay
packets are prepared for separate historical evaluation, not accepted as timely
live decisions. JSON validity and valid references do not establish semantic
correctness. An AI response cannot change prices, create new source IDs, access
a broker, or approve unavailable risk/position gates.

## What remains before autonomous operation

1. Validate the still incomplete corpus, visual anchors and conflicting source
   variants; benchmark retrieval and explicit context judgments on labelled cases.
2. Connect verified dated-contract definitions, timestamped executable quotes,
   reliable calendar/news and actual account/order reconciliation evidence.
3. Evaluate real model responses under an explicitly bounded provider budget.
4. Validate context/model selection and source-specific entry/exit compilers on
   untouched data; compare costs and execution against the fixed baselines.
5. Run forward paper and broker-simulator integration against actual feed events,
   then independent risk/operational review and explicit live authorization.

Existing `limited_live.py`, `broker_feedback.py`, `execution_ledger.py` and
`paper_lifecycle.py` remain the execution integration points. They have not been
enabled or replaced. A live backend is deliberately not wired by this prototype.
