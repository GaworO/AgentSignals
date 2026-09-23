# Continuation LONG/SHORT — integracja live 100K i 50K

Kod obsługuje oba konta w istniejącym układzie dwóch osobnych serwisów. Każdy
serwis ma własny `DATA_DIR`, Guard, stan, limit ryzyka i `EXEC_WEBHOOK`.

## Właściwości bezpieczeństwa

- LONG i SHORT korzystają z zamrożonego Entry, strukturalnego SL i przyczynowego
  celu OPEN DOL.
- Każdy kandydat przechodzi przez aktualny account-local Guard: świeżość feedu,
  sesję, news, equity/floor, limit dzienny, loss streak, jedną otwartą pozycję,
  cooldown oraz deduplikację.
- Maksymalne ryzyko Continuation wynosi $500 na 100K i $250 na 50K. Można je
  obniżyć zmiennymi środowiskowymi, ale kod nie pozwala ich przekroczyć.
- Nie stosuje się A/B partial, BE ani managera. Broker otrzymuje jeden bracket z
  oryginalnym celem OPEN DOL.
- Pierwszy skan po wdrożeniu tylko uzbraja adapter po ostatnim barze. Żaden
  historyczny lub zaległy order z bazy shadow nie zostanie wysłany.
- Każdy order ma trwały rekord dispatch. Błąd niejednoznaczny ma stan
  `SUBMISSION_UNKNOWN`, uruchamia CANCEL + EXIT i nigdy nie jest automatycznie
  ponawiany. Niepotwierdzony rollback zatrzymuje automat twardym Guard latch.
- Continuation i A/B korzystają ze wspólnej trwałej rezerwacji setupu, więc nie
  mogą jednocześnie zająć jednego konta.

## Zmienne na serwisie 100K

```text
CONTINUATION_SHADOW_ENABLED=1
CONTINUATION_LIVE_LONG=1
CONTINUATION_LIVE_SHORT=1
CONTINUATION_RISK_USD_100K=500
CONTINUATION_HTF_THESIS_MODE=allow_none
```

Pozostałe wartości (`EXEC_MODE`, `EXEC_WEBHOOK`, `ACCOUNT`, Guard, kontrakt i
equity sync) pozostają wartościami istniejącego serwisu 100K.

## Zmienne na serwisie Builder 50K

Szablon `RAILWAY_VARIABLES_BUILDER50.txt` zawiera już:

```text
CONTINUATION_SHADOW_ENABLED=1
CONTINUATION_LIVE_LONG=1
CONTINUATION_LIVE_SHORT=1
CONTINUATION_RISK_USD_50K=250
CONTINUATION_HTF_THESIS_MODE=allow_none
```

`CONTINUATION_HTF_THESIS_MODE=strict` zachowuje oryginalny filtr: LONG wymaga
tezy LONG, a SHORT tezy SHORT. `allow_none` dopuszcza wyłącznie neutralny stan
`NONE`; teza przeciwna do kierunku nadal blokuje zlecenie. Nie istnieje tryb,
który automatycznie dopuszcza przeciwną tezę.

Nie wolno współdzielić webhooka TradersPost ani `DATA_DIR` pomiędzy kontami.

## Widoki

- `/guard` — wysłane i zablokowane decyzje Continuation razem z A/B.
- `/continuation/live` — read-only ledger dispatch: `SENT`, `BLOCKED`,
  `DISABLED`, `SUBMISSION_UNKNOWN`, `ERROR`.
- `/all/trades` — Continuation LONG jako `CONT-L`, SHORT jako `CONT-S`.
- `/all/candidates` — oba kierunki w jednej tabeli kandydatów.
- `/continuation/dashboard` — pełny funnel oraz wyniki shadow.

## Uruchomienie

Najpierw wdrożyć z `CONTINUATION_LIVE_LONG=0` i
`CONTINUATION_LIVE_SHORT=0`, sprawdzić `/continuation/live`, `/guard/health`
oraz dwa różne `route_id`. Następnie włączyć kierunki przy `EXEC_MODE=manual`.
Po potwierdzeniu geometrii Entry/SL/TP i właściwego konta można przełączyć dany
serwis na jego normalny `EXEC_MODE=auto`.
