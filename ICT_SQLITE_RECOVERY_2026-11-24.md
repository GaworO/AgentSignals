# SQLite / ICT Observe — 24 listopada 2026

**Odzyskano produkcyjny Observe.** Poprawka `80eee2c` działa w wydaniu Railway `890719ce-8d3e-4d9a-9d5e-a57c51e3ebb2` (SUCCESS 12:46:29 UTC). ICT pozostał OFF do zabezpieczenia danych, podmiany i sprawdzenia wdrożonych plików. Wznowienie wyłącznie OBSERVE: 12:49:09 UTC. Pierwsza nowa świeca zamknięta 12:50 UTC została dopasowana w źródle, skanerze i rzeczywistym panelu. Ostatnia kontrola 12:52:45 UTC: `quick_check=ok` źródła, zachowanego Observe i nowego dziennika; **6 310 088 704 B dostępnych**. Bez nowej sesji shadow, płatnego LLM i zleceń w tej procedurze.

## Przyczyna i duplikaty

Backfill przetworzył 20 959 minut od 2026-10-27 05:00 UTC do 2026-11-17 19:16 UTC (62 877 rekordów ES/NQ/MNQ). Logi kończą pracę obserwatora błędem pełnego dysku o 07:38 UTC 24 listopada. Także zapis statusu zawiódł przez ENOSPC i zakończył wątek.

W bazie obserwatora nie znaleziono zduplikowanych kluczy `(symbol, contract, ts)`. Głównym problemem jest powielanie notatek audytu wewnątrz różnych wyników wyszukiwania wiedzy. 4 479 wyników ma różne pełne treści i identyfikatory, dlatego dotychczasowa deduplikacja całych wyników nie usuwała tego powtórzenia.

- 46 003 wystąpienia notatek, 59 unikalnych treści.
- Powtarzane notatki: 1 980 675 630 B; unikalne: 685 933 B.
- Tabela `retrievals`: 2 089 148 416 B, `snapshots`: 361 385 984 B, `bars`: 16 142 336 B (pomiar `dbstat` kopii po odtworzeniu WAL).
- Wzrost względem istniejącego backupu: nowa baza Observe około 2,57 GB; źródłowa baza świec tylko 184 320 B.

## Pojemność i backup

Użytkownik sam potwierdził przejście na Pro; API potwierdza plan PRO. Wolumin ostatecznie powiększono do 10 000 MB. Pomiar systemu plików: 9 759 125 504 B pojemności i 4 920 545 280 B dostępnych przed nowym backupem. Nie zmieniano planu ani nie kupowano dodatkowej usługi w tej procedurze.

Najmniejszy bezpieczny wariant wyliczony przed powiększeniem wynosił 7 GB nominalnie. Szczyt obliczony na podmianę z poprzednim stanem i 512 MiB rezerwy: 6 611 727 971 B plus narzut systemu plików. Pełny backup oszacowano na 856 MB, kopię kompaktową na 590 843 904 B. To miejsce robocze odzyskiwania, a nie wymaganie samego skanowania świec. Railway podaje 0,15 USD/GB-miesiąc rzeczywiście zajętej przestrzeni; Pro kosztuje 20 USD/miesiąc z kredytem na zużycie według planu. Źródła: https://docs.railway.com/pricing/plans oraz https://docs.railway.com/volumes/reference . Nie jest to pomiar końcowego rachunku.

Wcześniejszy backup 261 008 528 B sprzed awarii nie zawierał nowego Observe. Po odzyskaniu miejsca uruchomiono istniejące `python backup_retention.py --root /data/builder50`. Wynik: `verified=true`, 856 742 853 B, 913 plików, 42 bazy SQLite, retencja 1. Istnieje tylko `backups/recovery-current.tar.gz`; nie tworzono dodatkowych katalogów `pre-*` ani harmonogramów. Backup wykorzystuje spójne snapshoty SQLite, nie jest atomową kopią całego systemu handlowego.

Przed jakąkolwiek podmianą zabezpieczono również surowy Observe wraz z WAL i SHM poza Railway, w trwałym lokalnym `runtime-recovery/ict-observe-current/observe-current-raw.tar.gz`: 596 460 590 B, SHA-256 `4d58c39c81e5f51949172d5f7fd8628d75c2082399cba01b70d88cac589e416a`. Pliki sprawdzono po pobraniu, a następnie rzeczywiście odtworzono ze strumienia tej trwałej kopii w osobnym obszarze tymczasowym. Sumy wszystkich surowych plików zgodne, `quick_check=ok`, 20 959 partii i NQ do 2026-11-17 19:16 UTC. Kopia ani dzienniki nie trafiają do repozytorium.

## Bezstratna podmiana

Baza po odtworzeniu WAL: 2 571 026 432 B. Przygotowany cel: 590 843 904 B. Oszczędność: 1 980 182 528 B (77%). Wspólne notatki przechowywane raz według hasha; uporządkowane referencje odtwarzają dokładnie pierwotną treść. Snapshoty i pozostałe tabele niezmienione. Wszystkie 12 pierwotnych tabel ma identyczne liczby rekordów i kanoniczne hashe; retrievals porównywane po pełnym rozwinięciu. Bieżąca produkcja z 12:12 UTC jest identyczna z pierwszą kopią — brak pominiętych późniejszych zapisów.

Wykonana końcowa procedura dodatkowo odtworzyła Observe z nowego zachowanego backupu, porównuje backup/bieżącą bazę/kandydata pod blokadą i OFF, sprawdza brak otwartych deskryptorów Observe, kopiuje i fsyncuje kandydata na woluminie. SQLite sam wykonuje checkpoint i przechodzi w DELETE; dzienniki nie są ręcznie usuwane. Dopiero po zniknięciu dzienników przez SQLite następuje atomowe `os.replace` i kontrola odczytu. VACUUM wykonano wyłącznie wcześniej na nowej kopii offline z zapasem miejsca.

## Poprawka i testy

Punkt wyjścia: aktualna produkcja `936ee8943202e731825d9b528eaa4ded290f614a`, gałąź `shadow-railway-integration-20261123`, zachowany przodek Knowledge `fafc6271b2c8fcf0ac9db0f34a9e67b7962310a7`. Przed push ponownie fetch/porównanie. Automatyczny trigger usługi wskazuje inną gałąź; release przez jawny upload CLI aktualnego checkoutu.

- ICT wstrzymuje własne zapisy poniżej 512 MiB, przed tworzeniem plików i każdą transakcją; status pozostaje w pamięci, gdy nie wolno zapisywać na dysk.
- Automatyczne nadrabianie ograniczone do ostatnich 300 sekund i 5 partii/poll. Starsza historia nie jest odtwarzana; informacja o granicy jest jawna.
- Hashowana normalizacja notatek jest bezstratna, obsługuje stary format i odrzuca uszkodzone/brakujące referencje.
- OBSERVE nie inicjalizuje modułu płatnego LLM. Manifest v3 wymaga nowego katalogu bieżącego skanera; poprzedni dziennik pozostaje archiwum.
- 80 testów przeszło: normalizacja, uszkodzone referencje, rzeczywisty skaner przy niskim miejscu (brak zmian plików DB/WAL/SHM), start bez plików, wznowienie, ograniczony backfill i dedupe po restarcie, natywna integracja, Knowledge i legacy Shadow. `git diff --check` i chroniona baza kodu PASS.

Zatrzymano wyłącznie ICT przez prywatne `ICT_AI_MODE=OFF`; wspólny Gunicorn/ingest pozostał uruchomiony. Odczyt źródła po resize potwierdził NQ `CME_MINI:NQ1!`, start 1795523400, end 1795523460, confirmed/standard true. To etykieta ciągła, nie potwierdzona seria z terminem wygaśnięcia.

## Wykonany dowód produkcyjny

Hashe wdrożonych `runtime.py`, `bridge.py`, `storage_guard.py` i `retrieval_storage.py` zgodne z wydaniem. W trybie OFF po wdrożeniu oba zwykłe odczyty i quick_check przeszły. Następnie atomowo ustawiono tylko prywatne klucze ICT: OBSERVE, nowy `observe-recovered-20261124/observer.sqlite3`, 512 MiB, 300 sekund, 5 partii. Pozostałe klucze zachowano; `ICT_AI_PAID_APPROVED=false`. Gunicorn HUP wznowił worker bez zmiany flag trading/risk.

| Etap | Ten sam instrument i świeca | Wynik |
|---|---|---|
| Źródło `collection_bars` | `CME_MINI:NQ1!`, NQ M1, start `1795524540`, koniec `1795524600` | confirmed=true, standard=true; odbiór 12:50:00.724 UTC |
| Skaner | ten sam kontrakt i timestamp, identyczne OHLC | snapshot `3bc58d20dec2e5c0ed728458`, as_of 12:50:03 UTC, `NO_TRADE` |
| Produkcyjny Observe, API i widoczny panel | ten sam snapshot, `CME_MINI:NQ1!`, M1; oś 07:49 i koniec 07:50 NY | `NO_TRADE`, `NOT_CALLED`; zapis #6, zrzut zachowany |

To pierwsza świeca **zamknięta po wznowieniu 12:49:09 UTC**, obejmująca 12:49–12:50 UTC (07:49–07:50 NY). Nie wybrano późniejszej wygodniejszej świecy. Wynik silnika ma rzeczywistą nazwę `NO_TRADE`, nie `NO_SETUP`: 0 aktywnych setupów, 0 kompletnych planów, brakujący kontekst `H4_WARMUP`. Nowy przebieg dopiero zbiera kontekst wyższych interwałów; nie jest to dowód pełnej gotowości strategii ani rentowności. Seria jest ciągła, mapowanie na konkretny termin wygaśnięcia pozostaje niezweryfikowane.

[Dokładny zapis w produkcyjnym Observe](https://agent-50k-production.up.railway.app/ict-shadow?view=ictai&ai_mode=observe&ai_symbol=NQ&ai_tf=1&ai_snapshot=3bc58d20dec2e5c0ed728458).

Start przetworzył pięć partii z 12:44–12:48 UTC, następnie nową 12:49. Przy pierwszej próbie: 6 partii / 18 rekordów ES/NQ/MNQ. Przy końcowej kontroli: 8 partii / 24 rekordy, do źródłowego zamknięcia 12:52 UTC. Manifest `agent50k-ict-observer-v3`: `BOUNDED_RECENT_WINDOW`, 300 sekund, 5 partii, `older_history=NOT_REPLAYED`. Stara historia 20 959 partii pozostała zachowana w kompaktowym archiwum. Nie uruchomiono pełnego replayu.

Aktywny dziennik: 1 011 712 B, WAL 0 B, SHM 32 768 B; `ai_reviews=0`, brak tabeli reasoning_reviews. Worker `OBSERVING`, data_fresh=true, missing_pair_count=0, llm=NOT_ENABLED. Publiczne uwierzytelnione API: paid_calls_enabled=false, orders_enabled=false. Widoczny w panelu licznik 46 wywołań należy do oznaczonego archiwalnego eksperymentu 20 listopada, nie do tej próby.

Źródło: 101 478 400 B + WAL 16 512 B + SHM 32 768 B. Zachowane Observe: 590 843 904 B. Jedyny backup na woluminie: 856 742 853 B. Końcowy system plików: 9 759 125 504 B total, 3 432 259 584 B used, 6 310 088 704 B available. W okresie próby ingest i skaner przechodziły do kolejnych minut; nie ustawiano dodatkowego harmonogramu.

Prywatne dowody (`first-candle-proof.json`, `final-health.json`, `swap-report.json`, `durable-restore-report.json`, zrzut `first-candle-observe.jpg`) są w trwałym lokalnym `runtime-recovery/ict-observe-current`, wykluczonym z repozytorium. Surowa kopia obejmuje oryginalne WAL/SHM, a retained backup zawiera spójny snapshot Observe; obie ścieżki odtworzenia sprawdzono. Rollback należy wykonać offline z ICT OFF; przy odtwarzaniu całego systemu obowiązuje uzgodnienie stanu brokera opisane w AGENTS.md. Nie przywracać starego pełnego replayu.

Ograniczenie poza zakresem tej poprawki: stare moduły `continuation-shadow` i `_bars` nadal zgłaszają błędy agregacji/CSV. Te same błędy potwierdzono w logach poprzedniego wdrożenia. Nie zmieniano ich flag ani danych; nie są błędami bieżącego Observe. Testy regresji i odzyskanie ICT nie oznaczają, że wszystkie niezależne moduły produkcji są bezbłędne.
