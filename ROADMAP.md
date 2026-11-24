## 24 November 2026 — storage recovery completed

- DONE: narrow ICT OFF; preserve raw DB/WAL/SHM durably and test real restore; identify replay and repeated audit notes.
- DONE: Pro/10 GB; verified single full backup; compare all original tables against current production and safely replace with the 591 MB candidate.
- DONE: deploy 512 MiB write pause, 300-second/five-batch catch-up and lossless note normalization; 80 tests pass.
- DONE: resume only OBSERVE; first post-resume NQ M1 close 12:50 UTC matches source/scanner/real Observe UI; actual NO_TRADE / H4_WARMUP. All DB quick_checks pass; 6,310,088,704 B free at 12:52:45 UTC.
- LIMITATION: new run accumulates higher-timeframe context; dated contract remains unverified. Unrelated legacy CSV/aggregation errors predate this deployment and were not changed.

No new shadow session, paid LLM or orders. See `ICT_SQLITE_RECOVERY_2026-11-24.md`.

# AI ICT Trader — plan oparty na dowodach

| Priorytet | Stan | Zakres i dowód |
|---|---|---|
| P0 | FIXED_AND_OBSERVED | Poprawka 8aaf569: ponad 21 min bez OOM/restartu; 48 równoległych żądań PASS. Historycznie: RAM do 7,20/8 GB; nowy OOM kill podczas preflight. Ustalić źródło wzrostu i potwierdzić stabilność przed nowym obciążeniem/restartem. |
| P0 | VERIFIED | Broker świeży i płaski w preflight; stary INFLIGHT należy do zabitego procesu. Zachowane rezerwy/nieznane odpowiedzi; brak aktywnego transportu. Bez ponawiania nieznanych żądań. |
| P0 | IMPLEMENTED_AND_TESTED_LOCALLY | Natywna zakładka, autoryzacja, zgodność tras Shadow/history/scanner, trwałe snapshoty i pamięć hipotez. |
| P0 | IMPLEMENTED_AND_TESTED_LOCALLY | Wspólne rezerwacje całego limitu 8 USD, deduplikacja, jawna standardowa taryfa, datowane okno NYAM, panel kosztów. Wyników fixture nie uznawać za realny model. |
| P1 | IN_PROGRESS | Produkcyjny build Python 3.12 i wdrożenie wraz z prywatną bazą. Runtime 3.12.16 sprawdzony, nowy obraz jeszcze nie zbudowany. |
| P1 | IMPLEMENTED_NOT_EMPIRICALLY_VALIDATED | Rzeczywisty transport LLM/RAG, źródła, przeciwne hipotezy i zmiany tezy. Autoryzacja jest; nowych płatnych wywołań dotąd zero. |
| P1 | PARTIAL | Świeżość feedu sprawdzona dla fragmentu NYAM. Brak audytu pełnej sesji, mapowania serii ciągłej na kontrakt i potwierdzenia opóźnienia giełdowego. |
| P1 | IMPLEMENTED_NOT_EMPIRICALLY_VALIDATED | Ciągła obserwacja i ograniczanie wywołań zdarzeniami; nie włączone na niestabilnej produkcji. |
| P1 | PARTIAL | RAG: 136/615 pełnych dostępnych tekstów, 2558 reguł. 337 pending, 2 partial, 136 missing, 4 quarantined; brak pełnej weryfikacji audio/wykresów i wszystkich konfliktów źródłowych. |
| P2 | NOT_EXECUTED | Dzisiejsza analiza oraz porównanie A/B/C do 15 przypadków. Dzisiaj ma priorytet; liczbę historycznych par dobierać do pozostałego wspólnego limitu. |
| P2 | PARTIAL | Replay ma testy czasu dostępności i restartu; pierwotne minuty archiwalne miały założoną dostępność przy zamknięciu. Dzisiejszy replay ma użyć rzeczywistych czasów odbioru. |
| P2 | BLOCKED_BY_EVIDENCE | 37 niejednoznacznych realizacji; potrzebne dane o sekwencji kwotowań/zleceń. OHLC nie rozstrzyga kolejki, spreadu i opóźnień. |
| P2 | NOT_IMPLEMENTED_AS_VALIDATED_PROGRAM | Niezależne etykietowanie poprawności ICT, nietknięty holdout, test po kosztach, forward paper o odpowiedniej liczebności. |
| P3 | IMPLEMENTED_TESTS_ONLY | Niezależne kontrole ryzyka i symulator — wymagają dowodów kwotowań, kontraktów, news i jawnej polityki paper. Żadnej gwarancji działania live. |
| P3 | OUT_OF_SCOPE | Nowy moduł nie jest podłączony do realnych zleceń. Integracja wykonawcza i autoryzacja handlu to odrębny etap po walidacji. |

Powrót do wdrożenia nie wymaga ponownej zgody na wcześniej zatwierdzone 8 USD ani na samo wdrożenie. Ewentualne zwiększenie kosztu infrastruktury, limitu API albo zmiana istniejących flag handlu wymaga osobnej decyzji.

## IFVG observation release — 2026-11-24

Prepared against fetched production branch df27b2b (runtime file hashes match active recovery deployment 890719ce / 80eee2c). Adds M1/M5 inversion, recross and subsequent retest journaling plus native Observe panel. All zones remain observation-only: selection/entry/position/stop/target UNKNOWN, no fill assertion. Preserves 512 MiB guard and bounded 300 second / 5 batch intake. Code-only manifest handoff preserves historical manifests and all evidence/memory; no replay or new run. Verified single recovery backup before deployment. Focused, restart/dedupe, storage and legacy tests run locally; production validation is recorded separately after deployment. No paid API or broker changes.
