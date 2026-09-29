# Paczka V3 ENTRY-ONLY + pozostałe strategie SHADOW

Podstawa: ZIP `AgentSignals-main - 2026-09-29T130249.951.zip`, SHA256 `2f7f813a16f08bf96139bd6d08a4e14e1ffcf4e1d4a1f6c0e51493905716509c`.

## Co ta paczka robi — i czego nie robi

Umożliwia wysyłkę tylko A/B Directional LONG/SHORT z dotychczasowymi wejściami LIMIT, pierwotnym SL i pełnym TP2R. Stare A/B, skonfigurowany Shallow, Continuation oraz DOL pozostają obserwacjami. Nowe obserwacje nie zużywają rezerwacji, ramp, limitu wejść, dziennych strat i serii strat konta. Historyczne rzeczywiście wysłane pozycje nadal są uwzględniane przez Guard — nie usuwamy ich ani nie zerujemy blokad.

**To nie pełny V3 z aktywnym managerem.** Manager nadal nie może automatycznie zamykać pozycji bez poprawnego, świeżego broker feedback oraz brakującej walidacji i integracji P&L. Pozostają `RELEASE_LIVE_READY=False` i blokada executor-a przy `AB_V3_MODE=LIVE`.

Kod detektora, reguły managera, source locks, zasady news/sesji/DD i limity ryzyka nie zostały zmienione. Bramka kontraktu i uzbrojenia jest zabezpieczeniem wykonania, nie nowym filtrem jakości setupu. M5/M15/H1 są agregowane z M1, więc nie wymagają trzech dodatkowych alertów Pine.

Obecny `ab_risk_config.py` w najnowszym ZIP ma `SHALLOW_RISK_SHARE=0`. Nie zwiększamy tej wartości: przy zerowej alokacji Shallow nie powstaje, mimo `AB_SHALLOW_ENABLED=1`. Jeśli ma pozostać osobnym benchmarkiem, jego obserwacyjną konfigurację trzeba ustalić osobno. Nie zmieniaj alokacji live na podstawie tej instrukcji.

## Etap 1 — wgraj i sprawdź, bez handlu

1. Na istniejącym `/guard` ustaw **MANUAL**, nie używaj HALT jako zamiennika: HALT może wysłać cancel/flatten. Sprawdź w Tradovate, czy nie ma otwartej pozycji ani oczekujących zleceń. Nie migruj ich do nowego managera i nie usuwaj historii.
2. Zrób kopię repo i trwałego wolumenu `/data`. Rozpakuj ZIP; katalogi `deployment`, `tests`, `static`, `templates`, `tradingview` zachowaj. Wgraj tylko zawarte zmienione/dodane pliki do głównego katalogu repo, w jednym commicie. Nie przesyłaj baz, archiwów i plików `.env`.
3. Dodaj zmienne z `V3_STAGE_VARIABLES.txt` do serwisu Builder 50K. Nie kopiuj całego starego szablonu konta — zachowaj swoje obecne, zweryfikowane limity i webhook. Na początku **V3_ENTRY_ARMED=0**. Ustaw silny `GUARD_TOKEN` minimum 32 znaki (zachowaj istniejący, jeśli spełnia wymóg), żeby publiczne trasy zmiany trybu/synchronizacji nie były otwarte. Token wpisuj tylko w Railway i swoim panelu; nie publikuj go ani URL zawierającego `?t=`.
4. Wdróż. `/status` powinien pokazać wersję `v31.24-v3-entry-only-shadow-isolation`. Otwórz `/ab/v3` oraz `/guard/data`: `execution.policy=V3_ONLY`, `entry_armed=false`, `active_manager_live=false`. Sprawdź zapisany przez Guard `mode=manual`; ma on pierwszeństwo przed zmiennymi.
5. Nowe Continuation powinny mieć `SHADOW` w `/continuation/live`. Nowe stare A/B powinny mieć `SHADOW · bez zlecenia` w tabeli Guard. Gdy V3 jest rozbrojone, jego nowe wejście ma `BLOCK: v3_entry_not_armed`. To celowa blokada. Nie wysyłamy starych kandydatów ponownie po uzbrojeniu.

## Etap 2 — sprawdź kontrakt i bracket

6. Sprawdź rzeczywisty aktywny kontrakt w Tradovate i odpowiadający mu symbol w TradersPost. `EXEC_TICKER` musi być jawnym MNQ, np. format `MNQZ2026` — to przykład formatu, nie automatyczny wybór kontraktu. **Nie używaj starego `MNQU2026` bez weryfikacji i nie używaj `MNQ1!`.** Ustaw `AB_V3_CONTRACT` dokładnie na tę samą wartość. Feed detektora M1 musi dotyczyć tego samego kontraktu; przed zmianą źródła nie łącz historii różnych kontraktów tak, jakby to był jeden instrument. Nowy kontrakt wymaga ponownego właściwego warm-upu detektora.
7. W TradersPost sprawdź jedną subskrypcję do właściwego Builder 50K, wyłączność używania konta/route, użycie ilości ze sygnału, limitu wejścia, SL i TP ze sygnału. Żaden stary alert/inna usługa nie może nadal handlować na tym samym koncie. Nie kopiuj webhooku na przyszłe drugie konto. Dopiero po rzeczywistym sprawdzeniu ustaw `AB_V3_EXCLUSIVE_ROUTE=1` oraz `AB_V3_DETECTOR_CONTRACT_VERIFIED=1`.
8. Zostaw `PRICE_OFFSET=0`, `POINT_VALUE=2`, `EXEC_TICK=0.25`. Wariant wymaga pełnego TP2R, strukturalnego SL i jednego bracketu bez partiali. Zachowaj aktualny limit `EXEC_MAX_QTY`; budżet Directional jest nadal ograniczany dotychczasowym adapterem do maks. $250 na 50K, nie jest gwarancją maksymalnej rzeczywistej straty przy poślizgu.
9. Wykonaj próbę na oddzielnym koncie paper/simulation pod Twoim nadzorem. Nie wystarczy webhook testowy: sprawdź realną w tej symulacji aktywację limitu, fill, ilość oraz oba zlecenia ochronne po fillu, zachowanie po SL/TP i brak podwójnej wysyłki po restarcie. Nie używaj `/exectest` do obchodzenia bramki — syntetyczne legacy próbki są w V3_ONLY blokowane.

## Etap 3 — nadzorowane wejścia live, dopiero po kontroli

10. Potwierdź jeszcze raz stan FLAT oraz brak oczekujących zleceń w Tradovate. Uzgodnij wcześniejsze transakcje przez istniejący `/guard/reconcile` i synchronizację rzeczywistego salda/floor na `/guard`. Nie resetuj istniejącego kill/loss latch tylko dlatego, że nowa strategia jest wgrana. Jeżeli stan blokady jest niejasny, pozostań MANUAL.
11. Jeżeli kontrola została wykonana, ustaw `V3_ENTRY_ARMED=1`; pozostaw `AB_V3_MODE=SHADOW`. Włącz Auto Submit tylko na właściwej subskrypcji TradersPost i przełącz Guard na AUTO. `EXEC_MODE=auto` nie zastępuje zapisanego trybu Guard; sprawdź `/guard/data` po zmianie.
12. Pierwszy nowy, dozwolony przez Guard setup powinien dać `SENT` z ilością i nazwą `A/B Directional LONG/SHORT`. **SENT = przyjęty sygnał, nie fill.** W Tradovate natychmiast sprawdź faktyczną ilość/fill/SL/TP. Do czasu feedbacku wymagany jest nadzór; model w tabeli nie potwierdza rzeczywistego P&L ani zaliczenia challenge.

Gdy trzeba zatrzymać nowe wejścia: `V3_ENTRY_ARMED=0` lub Guard MANUAL. Nie wyłącza to ochronnych cancel/flatten Guard dla wcześniej wysłanych zleceń. `EXEC_STRATEGY_POLICY=SHADOW_ALL` wyłącza wysyłkę wszystkich nowych wejść, zachowując obserwacje. Nie ustawiaj `LEGACY`, jeśli chcesz utrzymać wyłączność V3; brak zmiennej w kodzie zachowuje LEGACY dla zgodności wstecznej.

## TradingView 1s — opcjonalna obserwacja, nie wymóg wejść ENTRY-ONLY

Obecny alert M1 na `/bars` pozostaje podstawą detektora. Do obserwacji managera dołączony jest brakujący w ZIP plik `tradingview/tv_1s_minute_batches.pine`: standardowy wykres M1 fizycznego kontraktu, dostęp do danych 1S i rzeczywistych CME, osobny alert „Any alert() function call” na `/bars/1s`, osobny silny `TV_1S_TOKEN`. Ustaw `TV_1S_ENABLED=1`, `TV_1S_SYMBOL` dokładnie jak symbol w payloadzie, a po sprawdzeniu mapowania `AB_V3_TV_MAPPING_VERIFIED=1`. Token umieść lokalnie w TV/Railway, nie przesyłaj go w czacie. Tworząc alert, odtwórz go po zmianie ustawień skryptu.

Feed daje paczki po zamknięciu M1, nie strumień co sekundę. Luki i różnice wolumenu pozostają widoczne; nie dopisujemy sztucznych sekund. Sam zakup TV nie rozwiązuje braku broker feedback. Nie dodawaj 60 webhooków/min do TradersPost.

## Czy TradersPost może zwrócić fill? Co dalej z mailami

Oficjalna dokumentacja sprawdzona 29.09.2026: [Key Limitations](https://docs.traderspost.io/docs#key-limitations) mówi, że platforma nie udostępnia broker order IDs, position state ani account information do logiki strategii. [FAQ](https://docs.traderspost.io/docs/additional-information/faq) opisuje maile o nowych/nieudanych webhookach i transakcjach. Sam fakt otrzymywania maili nie potwierdza istnienia wychodzącego webhooku do naszej aplikacji. [Known Limitations](https://docs.traderspost.io/docs/learn/known-limitations) opisuje także przypadki późniejszego odrzucenia zlecenia bez powiadomienia.

Potrzebujemy zanonimizowanego przykładu Twojego maila o fillu i zamknięciu. Jeśli rzeczywiście zawiera fill price/qty/time/order ID, można przygotować odbiór tych powiadomień jako dodatkowy **audyt wykonania**. Nie mamy jeszcze przykładu ani testu opóźnień/kompletności, więc parsera nie dołączono i nie zamieniamy takich maili automatycznie w stan OPEN brokera.

Otrzymany podczas pracy fragment zawierał JSON `action=sell`, `orderType=limit`, `limitPrice`, `quantity`, bracket, `time`, `cancelAfter` oraz drugi JSON `action=cancel`. To wysłane instrukcje, nie stan zwrotny. `time` jest czasem sygnału, `limitPrice` zamówioną ceną, `quantity` zamówioną ilością. Fragment nie zawiera faktycznego fill price/time/quantity, identyfikatora wykonania ani potwierdzenia anulowania. Jeśli reszta maila ma sekcję Filled/Executed, należy przeanalizować ją osobno; nie zakładamy, że cały mail nie może zawierać takich danych.

W przykładzie entry=30642,25, SL=30671,75, TP=30610,50. Ryzyko=29,50 pkt, odległość TP=31,75 pkt, czyli 1,076R. TP2R dla tej geometrii wynosi 30583,25. Tekst alertu o 2R nie odpowiadał wysłanemu TP. 8 MNQ to nominalne ryzyko 472 USD przed kosztami i poślizgiem. Nowy Directional adapter zachowuje limit 250 USD na 50K, a bramka wymaga rzeczywistego TP2R w payloadzie. Nie uznajemy starego przykładu za pozycję wykonaną ani za transakcję V3.

Możliwy przepływ audytowy: mail TradersPost -> dedykowana skrzynka/filtr -> uwierzytelniony relay -> magazyn powiadomień. Zwykłe przekazanie maila do URL nie jest bezpiecznym potwierdzeniem. Relay musi sprawdzić zaufane pochodzenie/DKIM, identyfikator zdarzenia, konto, kontrakt, kierunek, ilość i czas; usuwać duplikaty i nie udostępniać linków/tokenów. Wiadomości opóźnione/niejednoznaczne są informacją audytową, bez prawa do zamykania pozycji.

Pełny manager wymaga dodatkowo aktualnych snapshotów otwartej pozycji, aktywnego bracketu, CLOSED/CANCELLED/FLAT i rzeczywistego P&L. W obecnym protokole świeżość snapshotu to maks. 2 sekundy przed wyjściem. Mail o pierwszym fillu nie dostarcza tego ciągłego stanu. Nie zmniejszamy wymogu świeżości, żeby zmusić mail do spełnienia bramki.

Jeżeli support TradersPost udostępni oficjalny, uwierzytelniony callback/API, trzeba otrzymać jego dokumentację i przykładowy payload, napisać adapter do istniejącego `/ab/v3/broker`, zintegrować rzeczywiste P&L/stan z Guard i sprawdzić przypadki częściowych filli, restartu, timeoutu, zmiany ilości oraz brakującego maila. Sam callback fillu nadal nie wystarcza do pełnego managera.

Tekst do wysłania samodzielnie do support TradersPost:

> I use TradersPost with Tradovate on an MFF Builder 50K account. Do you offer an official authenticated outbound webhook or API with actual broker fills, partial fills, filled quantity and average price, broker order/position IDs, active brackets, cancellations, current position/flat state and realized P&L? I receive email notifications, but need machine-readable broker-confirmed events for an external position manager. Please provide the documentation, event schemas, authentication/signature method, latency, retry semantics and availability for my account. I do not need another inbound signal webhook.

Nie wysłano tej wiadomości za Ciebie. Bez tej możliwości i bez dostępnego broker API pełny automatyczny manager pozostaje blokowany; wejścia z brokerowym SL/TP2R są osobnym, nadzorowanym wariantem.
