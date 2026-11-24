## 24 November 2026 — recovery completed; no pending approval

The user upgraded Railway to Pro; the volume is 10 GB. The calculated practical minimum for safe backup/swap/rollback was 7 GB, not a permanent requirement of the candle scanner. Verified backup and actual restore, lossless Observe replacement, tested patch deployment and first post-resume NQ M1 source/scanner/UI verification are complete. Available space: 6,310,088,704 B at 12:52:45 UTC.

OBSERVE runs with 512 MiB guard and bounded 300-second/five-batch catch-up. The first close after resume returned actual NO_TRADE with H4_WARMUP; context accumulation and continuous-to-dated contract mapping remain research limitations. No paid LLM, new shadow session, orders or trading/risk flag changes were performed. Historical experiment permissions below do not apply. See `ICT_SQLITE_RECOVERY_2026-11-24.md`.

# Decyzje i blokady — 20 listopada 2026

Zgoda już udzielona: wdrożenie natywnego AI ICT Trader na istniejący Railway i łącznie maksymalnie 8 USD przed podatkiem na realny eksperyment dzisiejszej sesji oraz historyczne ramiona B/C. Nie prosić o tę samą zgodę ponownie.

Blokada pamięci została usunięta: poprawka 8aaf569 działa bez OOM i restartów w zmierzonym oknie. Trwa wdrożenie natywnej integracji, potem autoryzowany eksperyment. Nie jest potrzebna nowa decyzja ani zwiększenie zasobów.

Nie ma obecnie gotowej propozycji nowej płatnej infrastruktury do zatwierdzenia. Najpierw potrzebna jest diagnoza pamięci obecnego procesu. Jeżeli rozwiązanie wymaga podwyższenia abonamentu, ingerencji w istniejący handel albo zwiększenia limitu 8 USD, przedstawić konkretną zmianę i jej koszt/skutek do osobnej decyzji.

Nowy handel rzeczywisty pozostaje poza zakresem. Nie wyłączać ani nie włączać istniejących flag produkcyjnych pod pretekstem wdrożenia lub cleanupu. Forward paper wymaga osobnej jawnej polityki badawczej oraz danych o kontrakcie/kwotowaniach.

## IFVG observation release — 2026-11-24

Prepared against fetched production branch df27b2b (runtime file hashes match active recovery deployment 890719ce / 80eee2c). Adds M1/M5 inversion, recross and subsequent retest journaling plus native Observe panel. All zones remain observation-only: selection/entry/position/stop/target UNKNOWN, no fill assertion. Preserves 512 MiB guard and bounded 300 second / 5 batch intake. Code-only manifest handoff preserves historical manifests and all evidence/memory; no replay or new run. Verified single recovery backup before deployment. Focused, restart/dedupe, storage and legacy tests run locally; production validation is recorded separately after deployment. No paid API or broker changes.

## Independent market intake — 2026-11-24

The 18:04 UTC MNQ gap previously discarded real NQ and ES candles. Independent bounded intake now keeps each actual receipt, detects same-minute late peers, and archives out-of-order historical receipts once without rewriting live frames or alerts. Synchronization is checked by explicit model requirements at assessment: the default NQ/ES comparison requires matching M1 times; MNQ is not a universal gate. Preserve existing 512 MiB / 300 seconds / 5 batch limits. Actual 15:00–19:00 receipt-time replay: NQ 240, ES 240, MNQ 239; complete NQ/ES H4, missing MNQ H4. Production history is not replayed or repaired by this release. Prior IFVG geometry, native panel, Knowledge and paid/broker code are preserved. New production validation recorded separately.
