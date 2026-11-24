## 24 November 2026 — recovery completed; no pending approval

The user upgraded Railway to Pro; the volume is 10 GB. The calculated practical minimum for safe backup/swap/rollback was 7 GB, not a permanent requirement of the candle scanner. Verified backup and actual restore, lossless Observe replacement, tested patch deployment and first post-resume NQ M1 source/scanner/UI verification are complete. Available space: 6,310,088,704 B at 12:52:45 UTC.

OBSERVE runs with 512 MiB guard and bounded 300-second/five-batch catch-up. The first close after resume returned actual NO_TRADE with H4_WARMUP; context accumulation and continuous-to-dated contract mapping remain research limitations. No paid LLM, new shadow session, orders or trading/risk flag changes were performed. Historical experiment permissions below do not apply. See `ICT_SQLITE_RECOVERY_2026-11-24.md`.

# Decyzje i blokady — 20 listopada 2026

Zgoda już udzielona: wdrożenie natywnego AI ICT Trader na istniejący Railway i łącznie maksymalnie 8 USD przed podatkiem na realny eksperyment dzisiejszej sesji oraz historyczne ramiona B/C. Nie prosić o tę samą zgodę ponownie.

Blokada pamięci została usunięta: poprawka 8aaf569 działa bez OOM i restartów w zmierzonym oknie. Trwa wdrożenie natywnej integracji, potem autoryzowany eksperyment. Nie jest potrzebna nowa decyzja ani zwiększenie zasobów.

Nie ma obecnie gotowej propozycji nowej płatnej infrastruktury do zatwierdzenia. Najpierw potrzebna jest diagnoza pamięci obecnego procesu. Jeżeli rozwiązanie wymaga podwyższenia abonamentu, ingerencji w istniejący handel albo zwiększenia limitu 8 USD, przedstawić konkretną zmianę i jej koszt/skutek do osobnej decyzji.

Nowy handel rzeczywisty pozostaje poza zakresem. Nie wyłączać ani nie włączać istniejących flag produkcyjnych pod pretekstem wdrożenia lub cleanupu. Forward paper wymaga osobnej jawnej polityki badawczej oraz danych o kontrakcie/kwotowaniach.
