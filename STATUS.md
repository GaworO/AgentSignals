## 24 November 2026 — production ICT storage recovery complete

Release `80eee2c` / Railway `890719ce-8d3e-4d9a-9d5e-a57c51e3ebb2` is deployed. Preserve/restore verification completed for the sole full recovery backup and an off-volume raw Observe/WAL/SHM copy. All 12 original tables match; lossless replacement shrank Observe from 2.57 GB to 590,843,904 B. Pro/10 GB capacity confirmed. Source, preserved Observe and the new live journal pass ordinary reads and quick_check.

OBSERVE resumed at 12:49:09 UTC with the tested 512 MiB guard and 300-second / five-batch catch-up. The first NQ M1 close after resume, 12:50 UTC (`CME_MINI:NQ1!`, start 1795524540), matches source, scanner snapshot 3bc58d20dec2e5c0ed728458 and the actual Observe UI. Actual result NO_TRADE / H4_WARMUP, LLM NOT_CALLED, orders 0. Full replay did not restart. At 12:52:45 UTC available space was 6,310,088,704 B; fresh subsequent candles continue. 80 focused/native/Knowledge/legacy Shadow tests passed. See `ICT_SQLITE_RECOVERY_2026-11-24.md` for evidence and unrelated pre-existing legacy parser limitations. Older paid permissions below do not authorize this recovery.

# AI ICT Trader — 20 November 2026

Memory hotfix `8aaf569` is deployed and stable in the measured window. The production limit is 8,000,000,000 bytes. One Gunicorn worker / eight HTTP threads previously loaded 394 full review JSON files (592 MB on disk, 1.143 GB decoded Python objects) independently for each reader. The corrected reader streams records and bounds the metadata cache. Handoff no longer materializes the iterator. No history or production flags were removed.

After the repair: no OOM or worker restart; whole-container and worker ranges are in `docs/ict-ai-trader/reports/deployment-20261120/memory-stability.json`. The isolated corrected reader used approximately 24 MB RSS, four concurrent traversals approximately 44 MB. The whole app remains larger. Under eight-way access full history can take about 40 seconds; this is a latency limitation.

Native integration is locally validated and being released after the memory repair. 278 relevant regression tests passed; another 27 focused tests passed, including external bounded replay, source immutability, runtime configuration and cache bounds. Paid calls remain 0 at this checkpoint. The shared $8 before-tax approval remains valid; no extra approval is needed. New researcher has no order transport. Existing legacy trading flags remain unchanged.

Private knowledge release `f674585237afa449a421` is installed and all 143 manifest files verified on Railway: 136 available transcript reviews, 2558 rules, 3824 passage references. No claim of watching all 615 videos.

November 20 feed has no missing NYAM minutes through 11:30 NY across NQ/MNQ/ES. Dated reference streams ESZ2026/NQZ2026/MNQZ2026 exist; price equivalence is still being checked. The separate external replay writer preserves original receipt times and does not load history into the HTTP worker. API calls and session analysis are NOT_ANALYZED at this release checkpoint.

See `NYAM_AI_ICT_2026-11-20.md`; it will be updated with actual paid outcomes after production health verification.

## IFVG observation release — 2026-11-24

Prepared against fetched production branch df27b2b (runtime file hashes match active recovery deployment 890719ce / 80eee2c). Adds M1/M5 inversion, recross and subsequent retest journaling plus native Observe panel. All zones remain observation-only: selection/entry/position/stop/target UNKNOWN, no fill assertion. Preserves 512 MiB guard and bounded 300 second / 5 batch intake. Code-only manifest handoff preserves historical manifests and all evidence/memory; no replay or new run. Verified single recovery backup before deployment. Focused, restart/dedupe, storage and legacy tests run locally; production validation is recorded separately after deployment. No paid API or broker changes.

## Independent market intake — 2026-11-24

The 18:04 UTC MNQ gap previously discarded real NQ and ES candles. Independent bounded intake now keeps each actual receipt, detects same-minute late peers, and archives out-of-order historical receipts once without rewriting live frames or alerts. Synchronization is checked by explicit model requirements at assessment: the default NQ/ES comparison requires matching M1 times; MNQ is not a universal gate. Preserve existing 512 MiB / 300 seconds / 5 batch limits. Actual 15:00–19:00 receipt-time replay: NQ 240, ES 240, MNQ 239; complete NQ/ES H4, missing MNQ H4. Production history is not replayed or repaired by this release. Prior IFVG geometry, native panel, Knowledge and paid/broker code are preserved. New production validation recorded separately.
