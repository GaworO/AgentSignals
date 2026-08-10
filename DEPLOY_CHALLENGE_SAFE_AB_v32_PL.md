# AgentSignals v32.3 — challenge-safe A/B w Auto-Executorze

Ten build wykonuje wyłącznie setupy A/B (klasa A i B/DIB) oraz ich opcjonalną
nogę A/B-shallow. C, F, AMD, ORB i FX nie są rejestrowane ani forwardowane.

## Co zostało naprawione

1. Guard ocenia dokładną ilość kontraktów po zaokrągleniu i wszystkich
   dozwolonych redukcjach. Executor nie może już zmienić quantity po decyzji.
2. Magnet, Select, dynamic risk i stałe `EXEC_QTY` nie mogą zwiększyć pozycji w
   `CHALLENGE_MODE=1`.
3. Projected drawdown sumuje ryzyko A/B i A/B-shallow oraz rezerwę na poślizg,
   prowizję i `DD_PROJECTED_EXTRA_USD`.
4. Każda noga ma trwały `execution_id`. HTTP 2xx z relaya nie jest uznawane za
   fill/cancel/close; źródłem prawdy jest callback brokera.
5. Brak lub stary snapshot brokera, nieznana pozycja, working order albo
   niezamknięty lifecycle blokują następne wejście.
6. Cancel/exit wymagają 2xx i mają retry. EOD nie jest oznaczony jako wykonany,
   dopóki nowszy snapshot nie potwierdzi `position_qty=0` i zero working orders.
7. Zapisy guarda są atomowe i rzucają błąd. Nieudana rezerwacja stanu blokuje
   send, a nieudany zapis po send uruchamia rollback.
8. `/bars` wymaga tokenu, kompletnego poprawnego OHLC, świeżego czasu i
   chronologii. Identyczny retry jest ignorowany, konflikt tego samego timestampu
   zatrzymuje AUTO.
9. PASS wymaga targetu, minimum dwóch dni, consistency <= 50% oraz statusu
   `passed` otrzymanego z brokera/platformy. Samo $106,000 nie zatrzymuje konta
   przedwcześnie.
10. A/B i A/B-shallow liczą się jako jeden setup dla `MAX_TRADES_DAY` oraz jako
    jedna strata do `DAY_LOSS_N`. Nawet gdy obie nogi zakończą się na SL, para
    zwiększa licznik strat tylko o 1. Limit dolarowy nadal sumuje rzeczywisty
    wynik obu nóg, a guard blokuje nową grupę, jeżeli jej pełny SL przekroczyłby
    `DAY_LOSS_USD`.
11. Dla challenge 100K trailing floor ma cap `$100,100`.

Zlecenia pozostają na istniejącej ścieżce `EXEC_WEBHOOK`. Read-only ekran
`/all/trades` nie jest executorem: jego plik `allview.py`, dane i zachowanie nie
zostały zmienione.

## Profil ryzyka 0,70%

- `RISK_PCT=0.35`: maksymalnie 0,35% konta dla zwykłego wejścia A/B
  (klasa A albo B/DIB — nie osobny budżet dla każdej klasy).
- `AB_SHALLOW_RISK_PCT=0.35`: maksymalnie 0,35% konta dla A/B-shallow.
- Łączny maksymalny budżet obu niezależnych nóg wynosi 0,70% konta, czyli
  około `$700` przy koncie `$100,000`, przed zaokrągleniem liczby MNQ w dół.
- Guard dodaje koszty, oczekiwany poślizg oraz `DD_PROJECTED_EXTRA_USD=250`
  do kontroli projected drawdown. Ta rezerwa nie zwiększa quantity.
- W `CHALLENGE_MODE=1` Magnet, Select, dynamic risk, mnożnik sesji i ręczne
  `EXEC_QTY` nie mogą zwiększyć pozycji ponad te dwa budżety.

## Wymagany callback z istniejącego relaya/brokera

Bez tej integracji build celowo pozostanie fail-closed. Relay powinien zachować
`execution_id` umieszczony w polu `text` zlecenia i wysyłać lifecycle do:

`POST /broker/callback`

Nagłówek:

`X-Broker-Token: <BROKER_CALLBACK_TOKEN>`

Minimalne zdarzenie:

```json
{
  "event_id": "unikalne-id-zdarzenia",
  "execution_id": "exec_...",
  "order_id": "broker-order-id",
  "status": "working",
  "event_ms": 1786377600000
}
```

Dozwolone statusy: `working`, `partial`, `filled`, `canceled`, `rejected`,
`closed`, `expired`. Przy `closed` dodaj `realized_pnl`.

Relay/bridge powinien także wysyłać pełny snapshot co 30–60 sekund:

`POST /broker/sync`

```json
{
  "snapshot_id": "unikalne-id-snapshotu",
  "account_id": "zgodne-z-BROKER_ACCOUNT_ID",
  "as_of_ms": 1786377600000,
  "equity": 100250.00,
  "balance": 100100.00,
  "position_qty": 0,
  "working_orders_count": 0,
  "daily_realized_pnl": 100.00,
  "trading_day": "2026-08-10",
  "trading_days": 2,
  "best_day_profit": 850.00,
  "drawdown_floor": 97250.00,
  "evaluation_status": "active"
}
```

`evaluation_status` musi mieć wartość `active`, `passed`, `failed` lub
`breached`.

## Kolejność wdrożenia

1. Nałóż pliki z paczki na aktualny projekt AgentSignals; zachowaj pozostałe
   pliki detektora. Railway Volume musi pozostać podłączony jako `/data`.
2. Ustaw zmienne z `RAILWAY_VARIABLES_CHALLENGE_SAFE_AB_v32.txt`; wygeneruj
   oddzielne długie tokeny dla bars, callback i guard.
3. W TradingView ustaw URL `/bars?t=<BARS_TOKEN>` (TradingView nie pozwala
   ustawić własnego nagłówka). Inne feedy mogą użyć `X-Bars-Token` lub Bearer.
4. Pozostaw `EXEC_MODE=manual` i `AUTO_SUBMIT=0`.
5. Upewnij się, że relay/broker wysyła snapshot oraz lifecycle callback.
   `/broker/status` musi pokazać
   `fresh=true`, pozycję 0 i zero working orders.
6. Na demo sprawdź pełny cykl: planned -> working -> filled -> closed oraz
   rejected, partial fill, cancel, restart i EOD cleanup.
7. Uruchom `python3 -m unittest discover -v`; wszystkie testy muszą przejść.
8. Dopiero po poprawnym demo ustaw `EXEC_MODE=auto` i `AUTO_SUBMIT=1`.

## Krytyczne ograniczenie

Kod obsługuje i weryfikuje callback, ale sam nie może stworzyć danych, których
broker/relay nie wysyła. Jeśli używany relay nie potrafi zwrócić lifecycle,
pozycji, equity i drawdown floor, nie włączaj AUTO na challenge.
