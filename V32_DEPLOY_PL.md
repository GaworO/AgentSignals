# AgentSignals v32.0 - instrukcja wdrożenia

## 1. Który plik wybrać

Najprościej użyć pełnego repozytorium `AgentSignals-main-v32-final.zip`. Zawiera cały projekt i
wszystkie zmiany ExecutionPlan. Jeżeli aktualne repo ma własne, nowsze zmiany, użyj paczki
`AgentSignals-v32-update-only.zip` albo patcha i sprawdź konflikty.

## 2. Pliki, które muszą znaleźć się w root repozytorium

Nowe pliki:

- `execution_plan.py`
- `execution_engine.py`
- `broker_feedback.py`
- `timebase.py`
- `BROKER_CALLBACK_CONTRACT.md`
- `EXECUTION_PLAN_DEPLOY.md`
- `SESSION_POLICY.md`
- `V32_DEPLOY.md`
- `V32_DEPLOY_PL.md`
- `.env.v32.example`
- `tools/send_broker_event_test.py`
- testy w katalogu `tests/`

Pliki zastępowane przez wersję v32:

- `agent.py`
- `guardrails.py`
- `shadow.py`
- `manage.py`
- `allview.py`
- `ict/data.py`
- `model_c_live.py`
- `orb_live.py`
- `amd_live.py`
- `config/amd.yaml`
- `CHANGELOG.md`

Nie kopiuj samych `execution_plan.py` i `execution_engine.py`. Bez zmian w `agent.py`, `guardrails.py`,
`shadow.py` i `manage.py` plan nie będzie wspólnym źródłem prawdy.

## 3. Kopia danych trwałych przed wdrożeniem

Zrób kopię wolumenu lub pobierz:

```text
guard_log.json
guard_state.json
shadow_log.json
broker_unmatched.json
archive.csv
buffer.csv
sent_signals.json
trades.json
```

Nie nadpisuj ich plikami z ZIP-a. Paczka kodu nie zawiera Twoich live logów.

## 4. Wgranie do GitHub

### Pełne repo

1. Rozpakuj `AgentSignals-main-v32-final.zip`.
2. Skopiuj zawartość folderu do root repozytorium, tam gdzie znajduje się `agent.py`.
3. Zachowaj strukturę katalogów `detcore/`, `ict/`, `config/`, `tests/`, `tools/`.
4. Commit:

```bash
git checkout -b v32-execution-plan
git add .
git commit -m "v32: canonical ExecutionPlan and broker feedback"
git push -u origin v32-execution-plan
```

### Update-only

Rozpakuj `AgentSignals-v32-update-only.zip` bezpośrednio do root repo i zaakceptuj zastąpienie
plików. Nie usuwaj innych plików projektu.

### Patch

```bash
git checkout -b v32-execution-plan
git apply --check AgentSignals_v32_final.patch
git apply AgentSignals_v32_final.patch
git add .
git commit -m "v32: canonical ExecutionPlan and broker feedback"
```

## 5. Zmienne środowiskowe

Skopiuj ustawienia z `.env.v32.example` do Railway/Render. Najważniejsze:

```text
EXEC_MODE=manual
AUTO_SUBMIT=0
BUFFER_BARS=14000
EXEC_TICK=0.25
EXEC_TIF=day
FILL_WIN_MIN=10
EXEC_FILL_THROUGH_TICKS=1
EXEC_FILLBAR_TP=0
EXEC_ADVERSE_FIRST=1
PARTIAL_AT_1R=0
RAMP_TRADES=10
GUARD_TOKEN=<sekret>
BROKER_EVENT_TOKEN=<inny sekret>
BROKER_FEEDBACK_REQUIRED=1
BROKER_ACK_MAX_SEC=30
SKIP_SESSIONS=LO,ASIA,PREM,NYL
```

Nie ustawiaj `EXEC_PLAN_ENABLED` ani `EXEC_PLAN_SEND`: v32 nie używa tych flag. ExecutionPlan jest
zawsze aktywny dla głównej ścieżki A/B. Wysyłanie kontroluje `EXEC_MODE`.

## 6. Zegar

- Wszystkie timestampy są zapisywane jako UTC epoch milliseconds.
- Sesje strategii są interpretowane na stałym zegarze `UTC-04:00`.
- Zimą ten zegar nie jest tym samym co lokalny czas Nowego Jorku; to świadoma konfiguracja projektu.
- `cme_calendar.py` pozostaje w `America/Chicago`, bo kalendarz giełdy musi korzystać z czasu CME.

Nie ustawiaj strefy na podstawie lokalnego czasu serwera.

## 7. Broker feedback - element konieczny do pełnego działania

Po HTTP 2xx z `EXEC_WEBHOOK` guard wie tylko, że relay przyjął żądanie. Aby guard znał prawdziwy
status brokera, relay musi przesyłać callbacki do:

```text
POST https://TWOJ-AGENT/guard/broker-event?t=BROKER_EVENT_TOKEN
```

Callback musi zawierać co najmniej `plan_id`, `signal_key` albo znane `order_id`. Najlepiej zachować
identyfikatory umieszczone w polu `text` payloadu:

```text
[plan:<plan_id>] [signal:<signal_key>] [leg:<n>/<total>]
```

Minimalny callback:

```json
{
  "plan_id": "...",
  "order_id": "broker-123",
  "status": "accepted",
  "event_ms": 1785432000123
}
```

Pełny format jest w `BROKER_CALLBACK_CONTRACT.md`. Bez konfiguracji callbacku kod nadal może wysyłać
zlecenia, ale `/guard/health` pokaże brak broker feedback i guard nie będzie znał prawdziwego fill/P&L.

## 8. Test przed live

Lokalnie albo w CI:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

Oczekiwany wynik:

```text
Ran 18 tests
OK
```

Po deployu:

1. Otwórz `/status` i sprawdź `v32.0-execplan-broker-sync`.
2. Otwórz `/guard/health` - brak błędów kompilacji/config.
3. Pozostaw `EXEC_MODE=manual`.
4. Wyślij jeden testowy plan.
5. Sprawdź, czy w `/guard` pojawia się `plan_id` i `relay_status=accepted`.
6. Skonfiguruj callback i sprawdź sekwencję `accepted -> filled -> closed/canceled`.
7. Do testu callbacku użyj:

```bash
python tools/send_broker_event_test.py \
  --base-url https://TWOJ-AGENT \
  --token TWOJ_BROKER_EVENT_TOKEN \
  --plan-id PLAN_ID_Z_GUARDA \
  --status accepted
```

8. Dopiero po prawidłowym callbacku ustaw `EXEC_MODE=auto`.
9. Zachowaj `RAMP_TRADES=10`, aby pierwsze dziesięć wysłanych tradów miało 1 kontrakt.

## 9. Co sprawdzić po pierwszym prawdziwym tradzie

Porównaj w `/guard` i u brokera:

- `plan_id` i `order_id`;
- entry, SL i każdy TP;
- ilość kontraktów;
- `entry_ms`, `broker_accepted_ms`, `broker_active_from_ms`;
- rzeczywisty fill i realized P&L;
- zwolnienie one-position slot po `closed/canceled/rejected/expired`.

Jeżeli którykolwiek z tych elementów się różni, wróć do `EXEC_MODE=manual` i nie zwiększaj rozmiaru.
