# Wdrożenie A/B-shallow v31.5 na repo AgentSignals-main (65)

## Baza

Ta aktualizacja została zbudowana bezpośrednio na przesłanym repo:

- wersja wejściowa: `v31.4`;
- wersja po aktualizacji: `v31.5-ab-shallow-independent-risk2R`;
- brak zależności od `execution_plan.py`, `execution_engine.py`, `broker_feedback.py` i `timebase.py`.

## Pliki produkcyjne do podmiany

Skopiuj do root repo, obok `manage.py` i `shadow.py`:

1. `agent.py`
2. `guardrails.py`
3. `live_emit.py`
4. `ab_shallow.py` — nowy plik

`CHANGELOG.md` jest dokumentacyjny i może zostać podmieniony razem z nimi.

## Railway Variables

Wklej wartości z `RAILWAY_VARIABLES_AB_SHALLOW_v31_5.txt`.

Usuń, jeżeli istnieje:

```text
AB_SHALLOW_RISK_SHARE
```

Najważniejsze ustawienia:

```text
RISK_PCT=0.50
AB_SHALLOW_ENABLED=1
AB_SHALLOW_FRACTION=0.25
AB_SHALLOW_RR=2
AB_SHALLOW_RISK_PCT=0.50
```

Przy fillu obu zleceń maksymalne planowane ryzyko setupu wynosi 1.0% plus koszty.

## Kolejność

1. W `/guard/mode` przełącz executor na `manual` albo `off` na czas deployu.
2. Podmień cztery pliki produkcyjne.
3. Zaktualizuj Railway Variables.
4. Commit i push.
5. Poczekaj na zakończony deploy Railway.
6. Otwórz `/status`.
7. Otwórz `/guard/health`.
8. Po prawidłowej kontroli wróć do `auto`.

## Oczekiwany `/status`

```text
version = v31.5-ab-shallow-independent-risk2R
ab_shallow_enabled = true
ab_shallow_fraction = 0.25
ab_shallow_rr = 2.0
ab_shallow_risk_pct = 0.5
ab_shallow_combined_max_risk_pct = 1.0
```

## Zachowanie tabeli

Po wysłaniu setupu tabela `/guard` zapisuje kolejno:

```text
A/B
A/B-shallow
```

Każdy wiersz ma osobne entry, quantity, TP, wynik i klucz shadow. Oba mają ten sam `setup_group_id`.

## Guardrails

- para liczy się jako jeden setup dla `MAX_TRADES_DAY`;
- każda przegrana noga liczy się osobno do limitu dziennych strat;
- net P&L sumuje oba wiersze;
- jeżeli jedno zlecenie zostanie przyjęte, a drugie nie, system wysyła flatten+cancel;
- jeżeli stop shallow jest tak szeroki, że 1 kontrakt przekroczyłby 0.5%, shallow jest pomijane, a zwykłe A/B pozostaje dostępne;
- przy `AB_SHALLOW_DURING_RAMP=1` shallow działa od razu, także podczas `RAMP_TRADES`; override rampy pozostaje 1 kontrakt na każdy sibling. Ustaw `0`, aby rampa obejmowała tylko zwykłe A/B.

## Rollback

Najszybciej:

```text
AB_SHALLOW_ENABLED=0
```

To wyłącza sibling bez cofania kodu. Pełny rollback: przywróć cztery pliki z repo v31.4.
