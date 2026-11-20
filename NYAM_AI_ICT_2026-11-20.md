# NY AM AI ICT — 20 listopada 2026

**Aktualizacja: poprawka RAM wdrożona, ponad 21 minut bez OOM/restartu; natywna zakładka jest w przygotowaniu do wdrożenia. LLM jeszcze NOT_CALLED. Szczegóły aktualnej stabilności: `docs/ict-ai-trader/reports/deployment-20261120/memory-stability.json`.**

Poniższy pierwotny preflight zachowuje historyczne dowody blokady sprzed poprawki; końcowa analiza zostanie dopisana po rzeczywistych wywołaniach.
Użytkowniczka upoważniła wdrożenie oraz łącznie 8 USD przed podatkiem na nowy eksperyment, z priorytetem dzisiejszej sesji. Zgoda pozostaje zapisana; nie wymaga ponowienia. Nie autoryzowała nowych zleceń, zmian limitów ryzyka ani nowych usług.

## Blokada wdrożenia

Działające wdrożenie Railway `ee5447b2-592d-41a2-94da-be780ceef92f`, commit `75ad2a052a07c78ce52bb9d6945241be332ff9d9`, odpowiada bazie checkoutu i aktualnie pobranej gałęzi. Nie wykonano push, wdrożenia, restartu, zmiany zmiennych ani nowego backupu.

Limit RAM kontenera wynosi 8 000 000 000 bajtów. O 10:45:26 NY używał około 4,90 GB; o 10:46:21 NY około 7,20 GB. O 10:48:39 NY licznik `memory.events:oom_kill` wzrósł z 23 do 24, a pamięć spadła do 4,97 GB. Odczyty nie uruchamiały nowego obserwatora ani modelu. To dowód bieżącej niestabilności pamięci istniejącej produkcji; dokładna przyczyna wzrostu pamięci nie została jeszcze ustalona. Nie dokładano procesu budowy RAG ani nie wymuszano restartu usługi z aktywnym handlem.

Istniejące `LIVE_EXECUTION_ENABLED` i `ICT_EXECUTION_ENABLED` były już włączone. Pozostały bez zmian. Broker był świeży, bez pozycji i zleceń w momencie kontroli; nie jest to gwarancja późniejszego stanu. Nowy AI ICT Trader nie ma transportu zleceń. Istniały dwa wcześniejsze wywołania z nieznanym wynikiem po timeoutach i jedno dzisiejsze oznaczone `RESEARCH_INFLIGHT`. Nie ponowiono ich ani nie zmieniono ich ksiąg.

Logi Gunicorn potwierdzają SIGKILL i uruchomienie nowego workera o 10:38:52, 10:44:18 i 10:48:10 NY. [Zapis zdarzeń](docs/ict-ai-trader/reports/deployment-20261120/oom-events.json).

Lokalny pakiet regresji: **154 testy PASS w 98,01 s**, JavaScript syntax check i `git diff --check` poprawne. To nie jest wykonany produkcyjny build Python 3.12. Dodatkowe kontrole końcowe dotyczą uwierzytelnienia nowych endpointów oraz wymuszenia wspólnego limitu przed prawdziwym transportem.

## Wyniki wymagane dla sesji

| Obszar | Faktyczny wynik |
|---|---|
| 1. Źródło i pokrycie | Istniejący intake Tanja, `/data/builder50/market_context/tanja.sqlite3`, otwarty tylko do odczytu. NQ z `collection_bars`, MNQ/ES z `bars`. |
| 2. Dokładny czas obserwacji | Kontrola danych 10:46:21 America/New_York (15:46:21 UTC), 20 listopada 2026. Po 09:30 każdy instrument miał 76 zamkniętych minut, od 09:30 do 10:45. Nie zbadano jeszcze pełnego przedziału do 12:00. |
| 3. Wyższe interwały | Źródło zawiera starsze minuty od końca października. W tym zadaniu nie przeprowadzono jeszcze pełnego audytu ciągłości D1/H4/H1 ani analizy LLM. |
| 4. Płynność i zdarzenia | Nie obliczono dla dzisiejszego eksperymentu. Nie dopisano poziomów ani wydarzeń. |
| 5. Konkurujące hipotezy | Implementacja przygotowuje LONG/SHORT/NEUTRAL oraz scenariusze z detektora. Dzisiaj nie oceniono ich płatnym modelem. |
| 6. Wiedza ICT | Przygotowano nowy zweryfikowany lokalny release `f674585237afa449a421`: 136 dostępnych tekstów, 2558 reguł, 3824 referencje. Nie przeniesiono go jeszcze na produkcję. |
| 7. Faktyczne rozumowanie LLM | NOT_CALLED dla tego eksperymentu. Brak odpowiedzi i brak zastępczego opisu udającego odpowiedź modelu. |
| 8. Kandydaci | NOT_ANALYZED. Brak kwalifikowanych kandydatów z tego eksperymentu nie jest wynikiem NO_TRADE. |
| 9. Kwalifikacja/odrzucenie | Nie wykonano; nie można oceniać jakości scenariusza bez zapisanej analizy. |
| 10. Wejście/SL/TP/inwalidacja | Nie wyznaczono. Serie ciągłe nie potwierdzają tożsamości kontraktu wykonawczego. |
| 11. Chronologia decyzji | Zapisano kontrole gotowości i blokadę operacyjną; nie powstała chronologia decyzji LLM. |
| 12. Symulowane wyniki | Nie uruchomiono symulacji dzisiejszej transakcji. Nie dodano transakcji rzeczywistych. |
| 13. A/B/C | Dzisiejszego porównania nie wykonano. Wcześniejszy 15-przypadkowy pilot pozostaje niewykonany; przed użyciem trzeba ponownie zamrozić żądania z jawną standardową taryfą. |
| 14. Koszt | 0 nowych wywołań; 0 USD nowego eksperymentu. Wspólny limit 8 USD został zaimplementowany i objęty testami, ale nie aktywowany na produkcji. Wcześniejsze produkcyjne wywołania mają odrębną księgę. |
| 15. Ograniczenia | Niestabilna pamięć produkcji; nieukończony build Python 3.12; nieuzgodnione wcześniejsze wywołania; brak potwierdzonego mapowania kontraktów, uprawnień do danych bez opóźnienia i pełnej sesji; brak empirycznej oceny LLM/RAG. |

## Dane i granice wiarygodności

Zapisane tickery: `CME_MINI:NQ1!`, `CME_MINI:MNQ1!`, `CME_MINI:ES1!`. Nie utożsamiano ich automatycznie z grudniowymi kontraktami. Pola świec wskazują timeframe 1, confirmed=true, standard=true i zgodne granice minut. Opóźnienie zapisu względem zamknięcia świecy w sprawdzonym NYAM wynosiło około 0,37–19,49 s zależnie od instrumentu. To opóźnienie odbioru webhooka, **nie dowód braku opóźnienia giełdowego**. Źródło nie podaje deklaracji entitlement/delay.

Planowane resamplingi to M1/M5/M15/H1/H4/D1 wyłącznie z kompletnych zamkniętych minut. Brakujących świec nie wolno uzupełniać fikcyjnymi danymi. Jeżeli uruchomienie nastąpi po 12:00 NY, trzeba odtworzyć tę samą sesję 20 listopada chronologicznie według zapisanych czasów dostępności i oznaczyć ją REPLAY.

## Model i budżet

Potwierdzony model: `gpt-5.4-mini-2026-03-17`, Responses + structured outputs, reasoning medium. Standardowe stawki: 0,75 USD / milion tokenów wejściowych oraz 4,50 USD / milion wyjściowych; pomijamy rabaty cache przy wyznaczaniu kosztu maksymalnego. [Oficjalna dokumentacja modelu](https://developers.openai.com/api/docs/models/gpt-5.4-mini).

Nowy wspólny licznik obejmuje dzisiejszą analizę i historyczne ramiona B/C, niezależnie od ich dzienników i daty. Rezerwacja jest trwała przed wywołaniem; nieznany wynik zatrzymuje rezerwę. Identyfikator zgody i limit nie mogą być podmienione, żeby zresetować wydatki. Standardowa taryfa jest jawna. Nie ma automatycznych ponowień płatnych żądań. Panel posiada uwierzytelniony odczyt kosztów i raportu. Wywołania obserwatora ograniczono do jawnie zatwierdzonej daty i 09:30–12:00 NY, z odstępem co najmniej trzech minut i znaczącym zdarzeniem albo kontrolą co piętnaście minut.

## Kolejność dalszych prac

1. Zdiagnozować źródło wzrostu RAM istniejącego procesu Gunicorn i wykazać stabilność pod obciążeniem bez zwiększania abonamentu. Nie wyłączać automatycznie istniejącego handlu ani nie kasować danych.
2. Uzgodnić stan trwającego/nieznanych wywołań oraz ponownie sprawdzić bieżący broker przed restartem. Nie ponawiać ich w ciemno.
3. Zbudować i przetestować obraz Python 3.12; sprawdzić zgodność zapisanej i efektywnej konfiguracji. Runtime ma Python 3.12.16, ale nie ma jeszcze jsonschema; zależność jest w nowym requirements.
4. Po usunięciu blokad wykonać jedyny backup narzędziem `backup_retention.py`, ponownie pobrać gałąź i wdrożyć zweryfikowany release. Zachować obecną wersję do rollbacku i wszystkie dzienniki.
5. Przenieść prywatną bazę po hashach, uruchomić analizę dzisiejszej sesji, a następnie dobrać liczbę par B/C z pozostałego wspólnego budżetu. Dopiero wykonane wyniki uzupełnią ten raport.
6. Osobno ocenić trafność źródeł i hipotez, przeprowadzić nietknięty holdout i forward paper. Nie uznawać poprawnego interfejsu lub płynnych wyjaśnień za rentowność.

Szczegóły odczytów: [preflight.json](docs/ict-ai-trader/reports/deployment-20261120/preflight.json). Pełny stan dalszych prac: [ROADMAP.md](ROADMAP.md).
