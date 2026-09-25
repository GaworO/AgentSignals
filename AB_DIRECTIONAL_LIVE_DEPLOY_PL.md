# A/B Directional fixed 2R — wdrożenie LIVE

Nowa rodzina jest niezależna od zwykłego A/B i Continuation OPEN-DOL. Używa
tego samego causalnego łańcucha płynność -> displacement/FVG -> pullback/hold
-> BOS, ale bez filtra HTF i bez bramki DOL. Order ma Entry przesunięte o 1
punkt, strukturalny SL, stałe TP 2R, ważność 10 minut i fill jeden tick przez
Entry. Nie używa managera, partial ani BE.

## Railway 100K

```text
AB_DIRECTIONAL_LIVE_LONG=1
AB_DIRECTIONAL_LIVE_SHORT=0
AB_DIRECTIONAL_RISK_USD_100K=500
```

## Railway Builder 50K

```text
AB_DIRECTIONAL_LIVE_LONG=1
AB_DIRECTIONAL_LIVE_SHORT=0
AB_DIRECTIONAL_RISK_USD_50K=250
```

SHORT jest zakodowany, liczony i widoczny w tabelach, lecz domyślnie nie jest
wysyłany do brokera. Nie zmieniaj `AB_DIRECTIONAL_LIVE_SHORT=0` przy pierwszym
wdrożeniu.

Każdy nowy order przechodzi przez istniejący account-local Guard, jego filtry
sesji/news, limity MFF, loss streak, one-position, deduplikację i osobny webhook
TradersPost danego konta. Kod twardo ogranicza ryzyko do $500 na 100K i $250 na
50K nawet po wpisaniu większej wartości.

Widoki:

- `/continuation/live/dashboard` — decyzje LIVE, powód blokady, konto, quantity i route.
- `/continuation/candidates` — `AB_DIRECTIONAL` LONG i SHORT w funnelu.
- `/all/candidates` — `AB-DIR-L` oraz `AB-DIR-S`.
- `/all/trades` — zamknięte wyniki fixed 2R.

Pierwszy skan tylko uzbraja adapter. Historia nie jest wysyłana jako zaległe
zlecenia. Najpierw sprawdź widoki przy `EXEC_MODE=manual`, potem przełącz na
normalny `EXEC_MODE=auto` dopiero po potwierdzeniu route ID obu kont.
