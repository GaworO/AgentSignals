# Relay v32.1 — wdrożenie

## Co naprawia

- Nie traktuje TradersPost Signal ID jako broker order ID.
- Dodaje `cancelAfter` do każdego oczekującego zlecenia zgodnie z `ExecutionPlan`.
- Zachowuje `plan_id` i `signal_key` w `extras`.
- Zwraca AgentSignals wyłącznie status relay `accepted`, nigdy fałszywy broker fill.
- Udostępnia `/broker-event` dla prawdziwego callbacku z bezpośredniego API brokera.
- Udostępnia operatorowi `/reconcile-expired` po ręcznym sprawdzeniu konta.

## Railway

Utwórz osobny Railway Service z katalogiem `relay_service`.

Start command:

```text
gunicorn --bind 0.0.0.0:$PORT app:app
```

Ustaw zmienne z `.env.example`, a w głównym AgentSignals:

```text
EXEC_WEBHOOK=https://TWOJ-RELAY/stage?secret=RELAY_SECRET
BROKER_FEEDBACK_MODE=traderspost
BROKER_FEEDBACK_REQUIRED=1
```

`BROKER_FEEDBACK_MODE=traderspost` oznacza prawdę operacyjną: relay i automatyczne
`cancelAfter` działają, ale TradersPost nie udostępnia broker order ID ani statusu pozycji.
Aby używać trybu `direct`, trzeba podłączyć webhook lub API właściwego brokera do
`POST https://TWOJ-RELAY/broker-event?secret=BROKER_CALLBACK_SECRET`.


## Bezpieczny self-test

Po wdrożeniu wyślij POST do:

```text
https://TWOJ-RELAY/selftest?secret=RELAY_SECRET
```

Relay wysyła do TradersPost payload z `test=true`, więc nie składa zlecenia u brokera.
