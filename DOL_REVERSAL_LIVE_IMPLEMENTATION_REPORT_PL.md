# DOL Reversal LIVE — raport implementacji i testów

Status: **LIVE VIA TRADERSPOST WEBHOOK**

## Co zostało podłączone

- Kwalifikujący się A/B jest przed Guardem przeklasyfikowany na jeden `DOL_DELIVERY_REVERSAL`; nie powstaje drugi order.
- Entry i initial SL korzystają z tej samej istniejącej ścieżki wykonawczej. TP jest wymuszony na dokładnie `+2R`.
- Guard nadal oblicza ilość i rezerwuje pozycję osobno dla każdego konta.
- Entry, breakeven i full close idą przez `EXEC_WEBHOOK` danej usługi.
- HOLD nie wysyła webhooka. Protected stop jest wirtualny i po naruszeniu wysyła jeden deterministyczny `exit`.
- SQLite zabezpiecza atomową idempotencję; `audit.csv` jest jego czytelnym lustrem.
- Dashboard LIVE: `/dol-reversal-live`; dane JSON: `/dol-reversal-live/data`.

Frozen manager pozostaje `DOL_REVERSAL_MANAGER_58_V1`, threshold `0.892916`. Zweryfikowane hashe:

```text
model     13dc0cdc5044b86cb5c60796c760e98f08f234344fafcd4aa7ce53f65bf703e6
threshold 01af5db5980e9b0672a5e72d8ce33098dcb29934fcd48e6ed897db7b9559dfe8
```

## Routing 100K / 50K

Audyt konfiguracji Railway potwierdził:

| Konto | Usługa | Wolumen | Routing |
|---|---|---|---|
| 100K | `AgentSignals` | `/data` | osobny `EXEC_WEBHOOK` |
| 50K | `Agent 50k` | `/data/builder50` | osobny `EXEC_WEBHOOK` |

Skróty kontrolne wartości webhooków były różne (`39c63a5d0efb92b1` oraz `6ff3a264c59443fc`); pełnych sekretów nie zapisano w raporcie. Przed zmianą nie istniał `audit.csv`; były m.in. `guard_log.json`, `guard_decisions.jsonl`, `journal.db`, `continuation_live.sqlite3` i `broker_perf.csv`. Nowy audyt jest osobny per wolumen.

Przykład tego samego sygnału na obu kontach:

| Pole | 100K | 50K |
|---|---|---|
| `signal_id` | `DOLR-a5a41150f97f437f403e3561` | `DOLR-a5a41150f97f437f403e3561` |
| `client_order_id` | `DOLR-8decc036bec545b25e71a4ab` | `DOLR-17aaac76539361a102bb1d69` |
| lifecycle | `/data/audit.csv` | `/data/builder50/audit.csv` |

## Przykładowy lifecycle

1. A/B LONG: Entry `100.00`, SL `95.00`, ryzyko `5.00` pkt.
2. DOL kwalifikuje ten sam fizyczny order; TP zostaje ustawiony na `110.00` (`+2R`).
3. Przed POST powstaje rekord `PENDING` z `signal_id` i account-specific `client_order_id`.
4. HTTP 200 zapisuje `WEBHOOK_ACCEPTED` oraz TradersPost `id`/`logId`.
5. Późniejszy zamknięty bar przechodzi przez Entry o jeden tick: `LOCAL_FILL_DETECTED`, manager staje się aktywny.
6. Manager wybiera `VIRTUAL_PROTECTED_STOP=98.00`; initial/current physical SL nadal wynosi `95.00`.
7. Kolejny zamknięty bar narusza `98.00`; wysyłany jest dokładnie jeden payload `action: exit`, a stan przyjmuje `EXIT_REQUEST_ACCEPTED`.
8. Bez broker callback rzeczywista cena wykonania pozostaje nieznana i nie jest przedstawiana jako broker fill.

## Testy

Pełny wybrany zestaw: **38/38 PASS**.

Obejmuje parytet Continuation, parytet zwykłego A/B, jeden order DOL, fixed +2R, osobne client IDs, HOLD, breakeven, full close, virtual stop, restart/idempotencję, kill switch, `NO_OPEN_POSITION_AT_TRADERSPOST` i chronologiczny replay trzech barów przez prawdziwy endpoint `/bars`.

Pomiar lokalnej ścieżki bar→wywołanie webhooka, 50 prób, odpowiedź HTTP zamockowana (bez sieci TradersPost):

| Miara | p50 | p95 | max |
|---|---:|---:|---:|
| bar timestamp → rozpoczęcie POST | 1.014 ms | 1.450 ms | 1.610 ms |
| pełna lokalna funkcja z zapisem SQLite/CSV | 1.862 ms | 3.881 ms | 4.002 ms |

To jest latency aplikacji, nie end-to-end broker latency. Produkcja zapisuje `webhook_dispatched_at` i `bar_to_webhook_ms` dla każdej wysłanej akcji managera.

## Pliki zmienione

- `agent.py`
- `dashboard.py`
- `portfolio_guard.py`
- `dol_reversal_control.py`
- `dol_reversal_live.py`
- `dol_reversal_manager_shadow_v1.py`
- `tests/test_dol_reversal_activation.py`
- `tests/test_dol_reversal_live.py`
- `tests/test_dol_bars_endpoint.py`
- `DOL_REVERSAL_LIVE_TRADERSPOST_DEPLOY_PL.md`
- `DOL_REVERSAL_LIVE_IMPLEMENTATION_REPORT_PL.md`

Continuation nie został zmieniony funkcjonalnie i pozostaje bez managera.
