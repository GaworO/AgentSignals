# AgentSignals v32.1 — naprawa relay i statusu expired

## 1. Usuń obecny alarm dla potwierdzonego no-fill

Po sprawdzeniu na rachunku, że nie ma pozycji ani working order, uruchom na Macu w Terminalu:

```bash
python3 tools/expire_guard_order.py \
  --base-url "https://TWOJ-AGENT.up.railway.app" \
  --token "BROKER_EVENT_TOKEN" \
  --plan-id "ep_9aa079eee55f6cf74ab6fb2a" \
  --signal-key "A/B|SHORT|1785506820000|28229.49"
```

Oczekiwany wynik:

```text
HTTP 200
"matched": true
"status": "expired"
```

Następnie odśwież `/guard/health`. Stary alarm powinien zniknąć.

## 2. Wgraj poprawkę v32.1 do głównego AgentSignals

Zmienione pliki:

- `agent.py`
- `execution_plan.py`
- `broker_feedback.py`
- `guardrails.py`
- `.env.v32.example`
- `VERSION`
- `CHANGELOG.md`
- `tools/expire_guard_order.py`
- `tests/test_relay_fix.py`

W głównym serwisie Railway ustaw:

```text
BROKER_FEEDBACK_REQUIRED=1
BROKER_FEEDBACK_MODE=traderspost
FILL_WIN_MIN=10
```

Po deployu `/status` ma pokazać:

```text
v32.1-traderspost-relay-fix
```

## 3. Wdróż osobny relay

Utwórz osobny Railway Service z katalogiem `relay_service`.

Root Directory:

```text
relay_service
```

Start Command:

```text
gunicorn --bind 0.0.0.0:$PORT app:app
```

Zmienne relay:

```text
RELAY_SECRET=<długi sekret>
TRADERSPOST_WEBHOOK=<dokładny webhook strategii TradersPost>
TRADERSPOST_TIMEOUT_SEC=15
RELAY_CANCEL_AFTER_SEC=600
RELAY_SELFTEST_TICKER=MNQ1!
AGENT_BASE_URL=https://TWOJ-AGENT.up.railway.app
BROKER_EVENT_TOKEN=<ten sam token co w głównym AgentSignals>
BROKER_CALLBACK_SECRET=<inny długi sekret>
```

W głównym AgentSignals zmień:

```text
EXEC_WEBHOOK=https://TWOJ-RELAY.up.railway.app/stage?secret=RELAY_SECRET
```

## 4. Test bez składania zlecenia

Otwórz:

```text
https://TWOJ-RELAY.up.railway.app/health
```

Powinno być:

```json
{
  "ok": true,
  "version": "32.1",
  "traderspost_configured": true,
  "agent_callback_configured": true
}
```

## 5. Co zmienia się przy kolejnym tradzie

Payload zawiera:

```json
{
  "cancelAfter": 600,
  "extras": {
    "plan_id": "...",
    "signal_key": "...",
    "active_from_ms": 0,
    "valid_until_ms": 0
  }
}
```

W `/guard/data` powinno pojawić się:

```text
relay_status=accepted
relay_signal_id=<TradersPost Signal ID>
broker_order_id=null
```

To jest prawidłowe dla TradersPost. Prawdziwe `broker_order_id`, `filled` i `closed` są możliwe tylko
po podłączeniu bezpośredniego API lub webhooka brokera do `/broker-event` relay.


## 6. Bezpieczny test TradersPost bez zlecenia

```bash
curl -X POST "https://TWOJ-RELAY.up.railway.app/selftest?secret=RELAY_SECRET"
```

Wynik powinien zawierać `success:true` i `test:true`.
