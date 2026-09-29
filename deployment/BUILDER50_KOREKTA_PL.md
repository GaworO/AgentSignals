# Builder 50K — korekta istniejącego konta, bez resetu strat

Ta poprawka jest dodatkiem do wgranej paczki V3 ENTRY-ONLY z 29.09.2026.
Nie jest pełnym managerem V3 live. Zmieniono tylko guardrails.py i dodano testy/dokumentację.
Nie wysłano zleceń, nie zmieniono Railway ani bazy na serwerze.

## Co potwierdzają przesłane dane

- Evaluation, start 50 000 USD, cel 53 000 USD, start konta 30.08.2026.
- Widoczne saldo 49 637 USD, aktualny próg minimum balance 48 161,90 USD.
- Różnica do floor: około 1 475,10 USD; do celu: 3 363 USD. To nie budżet na jeden trade.
- 7 przesłanych wyników sumuje się do -363,00 USD: 2 zyski i 5 strat.
- Ostatnie 3 pozycje były stratne (-2,40, -0,90, -521,60), nie 4 jak grupy modelowe Guard.
  Pozycje 1-MNQ trwające kilka sekund mogą być testami, ale są realnymi stratami i ich nie usuwamy.
- Floor zaczynający około 48 000 USD pasuje do DEFAULT $2,000 MLL, nie Add-On $1,500.
  Przed użyciem potwierdź ten wariant w MFF. Nie używać dla Sim Funded.

Oficjalne zasady: https://help.myfundedfutures.com/en/articles/14290805-builder-plan-50k-a-comprehensive-guide

## Co naprawia kod

Dotychczasowy profil odrzucał DD_FLOOR inne niż dokładnie 48 000. Nowy akceptuje aktualny,
zweryfikowany próg 48 000..50 100, zachowując kontrolę pozostałych parametrów DEFAULT.
Nie obniżaj floor do 48 000 tylko po to, żeby usunąć ostrzeżenie.

Dodano POST /guard/account-realign oraz formularz w /guard. Wymagają silnego tokena,
MANUAL zapisanego w Guard, V3_ENTRY_ARMED=0, V3_ONLY, poprawnego profilu Evaluation DEFAULT,
zgodnego route, braku zobowiązań w Guard i ręcznego potwierdzenia stanu FLAT oraz braku
oczekujących zleceń w Tradovate. To potwierdzenie operatora, NIE automatyczny broker feedback.

Korekta zapisuje rzeczywiste equity/floor oraz referencję historycznego EOD high wynikającą
z floor + 2 000. Usuwa wpływ błędnej skali 100K na te wartości, ale NIE usuwa guard_log,
licznika ramp, kill/HALT, loss streak ani historii SENT. Najpierw zapisuje kopię starego stanu
w DATA_DIR/guard_account_realign_backup_*.json; błąd zapisu uniemożliwia udaną odpowiedź.
Nie jest to funkcja przeniesienia historii z innego konta ani inicjalizacji nowego konta.

## Krok po kroku

1. Pozostaw V3_ENTRY_ARMED=0. W /guard ustaw MANUAL. Sprawdź w Tradovate brak pozycji
   i zleceń oczekujących. Zrób kopię repo oraz całego trwałego wolumenu.
2. Podmień guardrails.py z tego ZIP. Testy i pliki deployment też możesz wgrać; zachowaj ścieżki.
3. W TYM serwisie Builder ustaw ACCOUNT_PLAN, START_BALANCE, TARGET_BALANCE itd. zgodnie
   z BUILDER50_PROFILE_VARIABLES.txt. Nie zmieniaj DATA_DIR ani istniejącego wolumenu.
   Zachowaj mocniejsze obecne limity Guard; nie włączaj AUTO i nie usuwaj blokad.
4. Przed użyciem DD_FLOOR=48161.90 ponownie sprawdź aktualny minimum balance w MFF.
   Jeśli próg już wzrósł, użyj nowszej wartości (do 50 100). DD_FLOOR nie jest saldem.
5. Zweryfikuj, że webhook TradersPost dotyczy dokładnie TEGO istniejącego Builder 50K.
   Sam napis ACCOUNT_LABEL niczego nie przekierowuje.
6. Wdróż. W /guard/data powinno być profile.plan=builder50, phase=evaluation,
   label=Builder 50K oraz profile_repair_version=builder50-profile-repair-v1.
   Jeśli pojawia się account_config warning, usuń przyczynę; nie obchodź Guard.
7. Otwórz /guard, używając swojego tokena w dotychczasowy sposób (nie udostępniaj URL/tokena).
   Rozwiń „Builder 50K — korekta profilu i rzeczywistego salda/floor”. Wpisz dokładne,
   świeżo odczytane saldo i floor z MFF (na zrzucie 49637 i 48161.90).
   Dopiero po wykonaniu wszystkich kontroli zaznacz potwierdzenie i naciśnij „Zapisz korektę”.
8. Sprawdź komunikat z nazwą kopii oraz /guard/data: mode=manual, equity_synced odpowiada
   MFF, verified_broker_floor odpowiada MFF, eval.floor i eval.buffer są w skali 50K.
   account_alignment.source wskazuje ręczny odczyt; to NIE potwierdzenie fillu.
9. NIE klikaj ARM tylko dlatego, że korekta się udała. Zachowane loss_streak/HALT mogą nadal
   blokować wejścia. Wyślij fragment /guard/data z loss_streak.recent, kill_reason,
   kill_until_ms i equity_synced (bez tokenów), aby dopasować 4 grupy do 7 prawdziwych pozycji.
   Do uzgodnienia potrzebny jest eksport Tradovate z czasami i identyfikatorami.
   Obecny importer z ostatniego ZIP dopasowuje kierunek/cenę/ilość bez kontroli daty;
   nie uruchamiaj jeszcze zbiorczego importu całej historii bez sprawdzenia dopasowań.
   Nie oznaczaj niewidocznego wpisu automatycznie jako no_fill/canceled.
10. Zwykłe codzienne /guard/sync stosuj dopiero po tej korekcie i uzgodnieniu transakcji.
    Po korekcie model dolicza wyłącznie P&L powstałe po snapshotcie. Bez API ten wynik pozostaje
    częściowo modelowy, więc snapshoty rzeczywistego salda i uzgodnienia są nadal wymagane.
11. Sprawdź jawny kontrakt TradingView M1 i TradersPost. Tradovate pokazuje MNQZ6;
    w poprzednim payloadzie TradersPost był MNQZ2026. W EXEC_TICKER i AB_V3_CONTRACT
    musi być ta sama poprawna dla TradersPost wartość; feed ma dotyczyć tego samego kontraktu.
12. Próbę paper i wyłączność route wykonaj jak w instrukcji ENTRY-ONLY. Dopiero po rozwiązaniu
    blokad i przejściu kontroli można uzbroić ENTRY-ONLY. AB_V3_MODE pozostaje SHADOW.

„cooldown 24h” w health opisuje ustawiony czas blokady, niekoniecznie pozostały czas.
Naprawa salda nie kasuje ani nie skraca tej ochrony. Nie zwiększaj GUARD_SYNC_MAX_H,
nie ustawiaj LOSS_STREAK_N=0 i nie usuwaj plików bazy/Guard, żeby uzyskać status zielony.
