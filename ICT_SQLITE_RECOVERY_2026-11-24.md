# SQLite / ICT Observe — 24 listopada 2026

Stan 12:40 UTC: dostęp do SQLite odzyskany po powiększeniu woluminu do 10 GB; obie produkcyjne bazy przechodzą zwykły odczyt i `quick_check=ok`. ICT pozostaje OFF do zakończenia podmiany i wdrożenia. Trwałe kopie są zabezpieczone. Nie wykonano jeszcze próby nowej świecy po wznowieniu.

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

Końcowa procedura dodatkowo odtwarza Observe z nowego zachowanego backupu, porównuje backup/bieżącą bazę/kandydata pod blokadą i OFF, sprawdza brak otwartych deskryptorów Observe, kopiuje i fsyncuje kandydata na woluminie. SQLite sam wykonuje checkpoint i przechodzi w DELETE; dzienniki nie są ręcznie usuwane. Dopiero po zniknięciu dzienników przez SQLite następuje atomowe `os.replace` i kontrola odczytu. VACUUM wykonano wyłącznie wcześniej na nowej kopii offline z zapasem miejsca.

## Poprawka i testy

Punkt wyjścia: aktualna produkcja `936ee8943202e731825d9b528eaa4ded290f614a`, gałąź `shadow-railway-integration-20261123`, zachowany przodek Knowledge `fafc6271b2c8fcf0ac9db0f34a9e67b7962310a7`. Przed push ponownie fetch/porównanie. Automatyczny trigger usługi wskazuje inną gałąź; release przez jawny upload CLI aktualnego checkoutu.

- ICT wstrzymuje własne zapisy poniżej 512 MiB, przed tworzeniem plików i każdą transakcją; status pozostaje w pamięci, gdy nie wolno zapisywać na dysk.
- Automatyczne nadrabianie ograniczone do ostatnich 300 sekund i 5 partii/poll. Starsza historia nie jest odtwarzana; informacja o granicy jest jawna.
- Hashowana normalizacja notatek jest bezstratna, obsługuje stary format i odrzuca uszkodzone/brakujące referencje.
- OBSERVE nie inicjalizuje modułu płatnego LLM. Manifest v3 wymaga nowego katalogu bieżącego skanera; poprzedni dziennik pozostaje archiwum.
- 80 testów przeszło: normalizacja, uszkodzone referencje, rzeczywisty skaner przy niskim miejscu (brak zmian plików DB/WAL/SHM), start bez plików, wznowienie, ograniczony backfill i dedupe po restarcie, natywna integracja, Knowledge i legacy Shadow. `git diff --check` i chroniona baza kodu PASS.

Zatrzymano wyłącznie ICT przez prywatne `ICT_AI_MODE=OFF`; wspólny Gunicorn/ingest pozostał uruchomiony. Odczyt źródła po resize potwierdził NQ `CME_MINI:NQ1!`, start 1795523400, end 1795523460, confirmed/standard true. To etykieta ciągła, nie potwierdzona seria z terminem wygaśnięcia.

## Pozostały dowód produkcyjny

Po podmianie i wdrożeniu: wznowić wyłącznie OBSERVE, zanotować rzeczywisty start procesu, wskazać pierwszą źródłową świecę NQ M1 zamkniętą później i dopasować kontrakt/timestamp do oceny skanera oraz produkcyjnego panelu Observe. Wymagany pomiar miejsca po próbie i potwierdzenie braku pełnego replayu. Płatny LLM, shadow i zlecenia wykluczone przez użytkownika.
