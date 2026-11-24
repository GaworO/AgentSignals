## 24 November 2026 — SQLite access recovered, release in progress

Pro and the 10 GB volume are confirmed. Ordinary production reads and both quick_checks now pass. ICT remains OFF. The sole retained production backup is verified (856,742,853 B / 913 files / 42 SQLite databases); the raw Observe DB/WAL/SHM has also been durably copied off-volume and actually restored/read successfully. All 12 original tables match the 590,843,904 B normalized candidate, with no later Observe writes omitted.

The prepared guard/bounded-catch-up/normalization fix passed 80 focused, native, Knowledge and legacy Shadow tests. Atomic replacement, release and the first post-resume NQ M1 source/scanner/UI proof are in progress. No shadow session, paid LLM or orders are authorized. See `ICT_SQLITE_RECOVERY_2026-11-24.md`; older experiment permissions below do not apply to this recovery.

# AI ICT Trader — 20 November 2026

Memory hotfix `8aaf569` is deployed and stable in the measured window. The production limit is 8,000,000,000 bytes. One Gunicorn worker / eight HTTP threads previously loaded 394 full review JSON files (592 MB on disk, 1.143 GB decoded Python objects) independently for each reader. The corrected reader streams records and bounds the metadata cache. Handoff no longer materializes the iterator. No history or production flags were removed.

After the repair: no OOM or worker restart; whole-container and worker ranges are in `docs/ict-ai-trader/reports/deployment-20261120/memory-stability.json`. The isolated corrected reader used approximately 24 MB RSS, four concurrent traversals approximately 44 MB. The whole app remains larger. Under eight-way access full history can take about 40 seconds; this is a latency limitation.

Native integration is locally validated and being released after the memory repair. 278 relevant regression tests passed; another 27 focused tests passed, including external bounded replay, source immutability, runtime configuration and cache bounds. Paid calls remain 0 at this checkpoint. The shared $8 before-tax approval remains valid; no extra approval is needed. New researcher has no order transport. Existing legacy trading flags remain unchanged.

Private knowledge release `f674585237afa449a421` is installed and all 143 manifest files verified on Railway: 136 available transcript reviews, 2558 rules, 3824 passage references. No claim of watching all 615 videos.

November 20 feed has no missing NYAM minutes through 11:30 NY across NQ/MNQ/ES. Dated reference streams ESZ2026/NQZ2026/MNQZ2026 exist; price equivalence is still being checked. The separate external replay writer preserves original receipt times and does not load history into the HTTP worker. API calls and session analysis are NOT_ANALYZED at this release checkpoint.

See `NYAM_AI_ICT_2026-11-20.md`; it will be updated with actual paid outcomes after production health verification.
