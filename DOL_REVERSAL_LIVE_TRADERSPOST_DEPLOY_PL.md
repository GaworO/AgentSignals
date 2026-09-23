# DOL Reversal + Manager — wdrożenie LIVE przez TradersPost

Status pakietu: **LIVE VIA TRADERSPOST WEBHOOK**

## Kolejność wdrożenia

1. Wgraj pliki z paczki update-only do obu usług Railway bez usuwania ich wolumenów `/data`.
2. Upewnij się, że każda usługa zachowała własny `EXEC_WEBHOOK`, `ACCOUNT_LABEL` i `DATA_DIR`.
3. Na obu usługach ustaw najpierw:

```text
DOL_REVERSAL_MODE=LIVE
DOL_MANAGER_MODE=LIVE
DOL_MANAGER_EXECUTION=TRADERSPOST_WEBHOOK
DOL_KILL_SWITCH=0
DOL_REVERSAL_MANAGER_SHADOW_ENABLED=false
DOL_TRADERSPOST_TEST=1
```

4. Sprawdź `/dol-reversal/readiness`. Oczekiwane: `LIVE READY`, poprawne oba hashe i brak activation blockers.
5. Sprawdź `/dol-reversal-live`. Testowa akceptacja ma być opisana jako `WEBHOOK_ACCEPTED`, nigdy jako fill.
6. Zweryfikuj `test:true` na właściwej paper/sim subscription TradersPost dla każdego konta. Nie wysyłaj ręcznego testowego orderu do realnego konta.
7. Po potwierdzeniu routingu paper/sim ustaw `DOL_TRADERSPOST_TEST=0`. Pierwszy realny order może pochodzić wyłącznie z naturalnego sygnału strategii.

Nie kopiuj `EXEC_WEBHOOK` pomiędzy usługami. W sprawdzonej konfiguracji `AgentSignals` (100K) i `Agent 50k` mają różne wartości webhooka i osobne wolumeny danych.

## Rollback

Na obu usługach ustaw:

```text
DOL_KILL_SWITCH=1
```

Blokuje to nowe wejścia DOL i nowe akcje managera. Nie anuluje istniejącego fizycznego SL. Pozycję już otwartą należy nadzorować po stronie TradersPost/brokera.

## Stan trwały i audyt

Każda usługa tworzy we własnym `DATA_DIR`:

- `dol_reversal_live.sqlite3` — autorytatywny, transakcyjny ledger i klucze idempotencji;
- `audit.csv` — czytelne lustro lifecycle aktualizowane po każdym zdarzeniu.

Nie dołączono pustego `audit.csv` do ZIP-a, ponieważ nadpisanie pliku na wolumenie mogłoby skasować historię. Kod tworzy go automatycznie z wymaganym nagłówkiem.

## Kontrola po wdrożeniu

- `WEBHOOK_ACCEPTED` oznacza tylko HTTP 2xx z TradersPost.
- `LOCAL_FILL_DETECTED` oznacza causal one-tick trade-through na zamkniętym M1.
- `BROKER_FILL_CONFIRMED` nie jest pokazywany, bo obecna integracja nie dostarcza takiego potwierdzenia.
- `VIRTUAL_PROTECTED_STOP` nie zmienia początkowego fizycznego SL.
- `EXIT_REQUEST_ACCEPTED` oznacza przyjęcie prośby `exit`, a nie potwierdzoną cenę wykonania.
- `UNKNOWN_REQUIRES_REVIEW` nie jest automatycznie ponawiany.

## Ograniczenia

- Brak broker order ID, broker position ID i broker fill callback.
- Lokalny fill i lokalne zamknięcie są estymacją z zamkniętych barów, z konserwatywnym SL-first przy konflikcie w jednym barze.
- Rzeczywista cena wyjścia i slippage pozostają puste, dopóki nie pojawi się broker confirmation. Zapisywana jest cena `close` baru, przy której wysłano virtual-stop exit.
- Virtual protected stop zależy od feedu, działania Railway i dostępności webhooka; initial SL pozostaje awaryjnym zabezpieczeniem po stronie brokera.
- Odpowiedź `NO_OPEN_POSITION_AT_TRADERSPOST` zamyka lokalną aktywność managera i wymaga ręcznej rekonsyliacji; system nie wysyła drugiego zlecenia.

