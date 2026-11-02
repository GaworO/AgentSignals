# ICT 2022 + IFVG review release

Adds IFVG assessment to the same cloud AI request and reservation as ICT 2022.
Original ICT 2022 prompt/price selection, entry rules, execution contract and
0.5% structural sizing are retained. No research entry-quality variant applied.

- The top-level assessment remains the existing ICT 2022 execution input.
- `ifvg_review` is a separately bound same-cutoff assessment. Closed opposite
  FVG formation plus later same-timeframe inversion close are required.
- Missing path evidence, re-inversion, stale response, invalid price, expiry,
  unsupported anchor or insufficient liquidity room block the IFVG proposal.
- IFVG shadow outcomes use fixed 2R, with AI's liquidity objective recorded.
- IFVG rows are dashboard projections of the saved parent response, not extra
  paid reviews. They never enter the raw-record execution queue. Explicit
  handoff rejection additionally protects direct companion submissions.
- IFVG has no broker permission in this release. Existing permission values
  are not modified. No automatic recovery/partials/trailing added.
- Same call/day/dollar caps, cooldown, production priority and separate ledger.
- New closed inversion observations can trigger the existing guarded scheduler.
- Authenticated `/ict-shadow/ifvg-examples` contains historical saved AI ideas
  with clearly labelled simulated outcomes, including an ambiguous loss.

Tests use mocked responses; a genuine new dual-family response must be observed
in an eligible production session before claiming live AI behavior verified.
