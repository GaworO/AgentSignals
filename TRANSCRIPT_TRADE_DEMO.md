# Historical transcript trade replay

Run from the existing checkout (no API key or network connection needed):

```bash
cd "/Users/aleksandra/Desktop/NQ Signals/AgentSignals-main"
services/tanja/live_transcription/.venv/bin/python services/tanja/transcript_trade_interpreter.py \
  --transcript "/Users/aleksandra/Downloads/NoteGPT_3 BREAKEVENS AGAIN....DAY TRADING ICT CONCEPTS _ 7_23_2026.txt" \
  --start 00:32:54 --end 00:41:13 \
  --output-dir services/tanja/live_transcription/output/transcript_trade_demo
```

This is a deterministic language interpreter, not an LLM. It loads the existing
Tanja vocabulary for terminology matches and provenance, never source-example
prices or reference event labels. Unsupported phrasing can be missed. One selected
historical episode was replayed; this is not a claim of accuracy across all sessions.

Outputs in the specified directory:

- `events.jsonl`: exact quotes, original line/sentence order, block timestamp,
  explicit fields, evidence status, reported state and immutable source/vocabulary hashes.
- `paper_actions.jsonl`: paper state after every event and individual rejection reasons.
- `shadow_observations.jsonl`: records read back from the existing Shadow journal.
- `summary.json`: counts, source identity, processing time and limitations.
- `journal/tanja.sqlite3`: isolated demonstration data using the existing Store
  and TranscriptJournal schemas; no new production database/service.

Reported state and our paper state are independent. A reported MARKET submission
is pending until independently acknowledged; a LIMIT fill report never fills us.
A second fill statement after entry can refine reported position details; it is
not a second execution. Pending limit modifications require a readable replacement
entry and stop and another risk check. Partial/full exit requests cannot create
fills. Explicit independent simulator acknowledgements are supported in the
Python interface and tested; the historical CLI does not fabricate them.

`PaperLifecycle` requires compiled strategy approvals. Those approvals are absent
from speech, so this demo uses a separate small PaperCopy state machine instead of
fabricating approval. It reuses `risk_sizing.risk_budget` and
`contracts_for_stop` (MNQ, $2/point, quarter-point ticks). Other instruments are
observations only; ES is never silently converted to MNQ or assigned an expiry.
Optional `--paper-account PATH` accepts an explicitly configured simulation-only
JSON with `balance`, `minimum_balance`, `daily_loss_remaining`, and
`committed_risk`; missing configuration rejects executable orders. Budget is the
minimum of 0.5% balance less committed risk, prop balance headroom, and daily loss
headroom. Quantity is capped downward; no forced minimum. Stop widening is rejected.
Account limits must be current for the historical scenario being simulated; this
CLI is not a live account verification mechanism. Initial sizing covers planned
price risk, not fees/slippage; independent fills exceeding the budget are rejected.

The only existing module changed is `transcript_ingestion.py`: a dedicated
`record_decision` method atomically stores decision events and non-executable
Shadow observations in the existing tables. Existing audio ingestion and Railway
startup are untouched. `/api/transcripts/shadow` already reads these records when
using the same Store. This isolated demonstration does not write to Railway.

Source timestamps are NoteGPT block starts, not word times. Events within a block
retain sentence order without invented timing. Plans do not become execution
parameters, and later details never backfill earlier event snapshots. The replay
uses source time for freshness checks; historical text must never be sent as a
fresh live signal. Extracted Shadow records also preserve actual receipt time.
Stable event/session IDs deduplicate saved decisions. Replaying an already
completed session reads its existing Shadow records without appending duplicates.
Interpreter semantics should be versioned with a new replay session when changed;
completed sessions are not rewritten.

## Measured demonstration

Two focused executions were performed: nine unit checks, then one historical CLI
replay. Nine checks passed in 0.040 s. They cover MARKET/LIMIT submission versus
independent fills, limit modification/cancellation, partial/full closes, stop
updates, risk/prop limits, stale/duplicate events, contradictions, ambiguous
numbers, HOLD, negation, causal field snapshots and Shadow persistence.

Historical replay: nine events, nine persisted Shadow observations, seven rejected
actions, zero submitted paper orders, zero independent fills. Processing and
persistence took 0.04554 s on this machine; this is not speech detection latency.

| Block start | Interpretation | Evidence/state |
|---|---|---|
| 00:32:54 | TRADE_PLAN | Conditional short idea; no order |
| 00:33:59 | LIMIT_ORDER_PLACED | “I have my limit in”; reported PENDING_ORDER |
| 00:35:29 | LIMIT_ORDER_FILLED | “Okay, I just got filled”; reported OPEN_POSITION |
| 00:37:31 | LIMIT_ORDER_FILLED | “I shorted ES”; additional position detail, not a new fill |
| 00:38:03 | PARTIAL_EXIT | Intended partial only |
| 00:38:03 | STOP_MOVED | Intended swing-high stop; numeric price UNKNOWN |
| 00:38:59 | FULL_EXIT | Conditional exit only; not closed |
| 00:40:20 | PARTIAL_EXIT | “I took some partials”; reported PARTIAL, exact quantity UNKNOWN |
| 00:40:20 | HOLD | Debating a runner; never an entry |

The placement was rejected for missing instrument, side, quantity, entry, stop,
and explicit paper-account/prop configuration. ES becomes explicit later without
retroactively completing the rejected order. Intended management actions are
non-executable; the reported partial cannot close a paper position that never
independently filled. No numerical entry/SL/TP was recovered from this episode.

Remaining limitations: conservative rule coverage, transcript-only evidence,
coarse timestamps, absent complete order numbers/account configuration, and no
independent simulation execution feed. Suitable for observation and research
review; not ready for live copying. No broker calls, paid APIs, trading integration
activation, autonomous strategy changes or deployment were performed.
