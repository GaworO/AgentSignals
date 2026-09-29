# Weryfikacja paczki — 29.09.2026

## Zakres

Wariant: **A/B Directional ENTRY-ONLY, SL/TP2R, pozostałe rodziny shadow**. Pełny manager LIVE nadal blokowany. Nie tworzono connectora brokera ani parsera maili bez przykładowych wiadomości i udokumentowanego źródła.

Paczka to tylko aktualizacje do ZIP 29.09, nie całe repo. Kod produkcyjny w głównym katalogu workspace nie został nadpisany. Zmiany wykonano w oddzielnej kopii najnowszego ZIP. Nie zmieniono Railway, TV, TradersPost, Tradovate ani baz konta; nie wysłano żadnego rzeczywistego zlecenia.

## Wyniki

- 140 wykonań testów wybranych modułów integracji i regresji: PASS, bez failures/errors. Nie jest to cały zestaw historycznych testów repo; zestaw zawiera też odziedziczone przypadki adaptera Continuation.
- Dodatkowo 3 testy A/B Directional w izolowanej przestrzeni importów detektora: PASS.
- Składnia Python i `node --check static/ab_v3.js`: PASS.
- SHA polityki managera niezmienione: `2e48d608220ecdff5e6da700568fca76d8196ea45b9cc4ef02ee4e47f9e668d4`.
- `RELEASE_LIVE_READY=False` zachowane; próba `AB_V3_MODE=LIVE` nie wysyła wejścia ani wyjścia.

Weryfikowano m.in. oba kierunki, brak wysyłki legacy A/B/Shallow/Continuation/DOL, odmowę przed rezerwacją sibling batch, brak promocji przez samą flagę Directional, brak wpływu hipotetycznych strat na Guard/ramp, jednokrotne zdarzenia shadow, blokowanie obcego/ciągłego kontraktu i złej geometrii/risk budget, wszystkie mockowane odmowy Guard, wymagane uzbrojenie oraz brak uznawania HTTP200 z `success=false` za SENT. Nieprawidłowa odpowiedź HTTP200 jest oznaczana jako niejednoznaczna; nie ma automatycznej ponownej wysyłki.

Testy używały tymczasowych baz, mocków HTTP i blokady połączeń. Potwierdzają logikę, nie gotowość rzeczywistego brokera, szybkość maili, przyjęcie bracketu przez Tradovate ani wyniki challenge. Próba paper i kontrola faktycznych zleceń pozostają obowiązkowe przed uzbrojeniem.

## Wykryte ograniczenia istniejącego repo

- Stare `tests/test_prem_stop_filter_v31_14.py` odwołują się do nieistniejącego w ZIP `guardrails.prem_stop_policy` i dają 6 errors. Nie dodano ponownie tego starego filtra i nie zaliczono tych testów jako PASS.
- Równoległy start odbiornika 1s ujawnił `database is locked` podczas ustawiania WAL. W dołączonym `tv_seconds_feed.py` zserializowano krótkie operacje SQLite w obrębie procesu; test konkurencyjnych duplikatów przechodzi. Nie jest to gwarancja działania wielu niezależnych replik/workers na współdzielonym SQLite.
- W ZIP brakowało Pine. Dołączono wcześniej używany skrypt minute-batch; nie zamieniono go w feed intraminutowy.
- `SHALLOW_RISK_SHARE=0` w najnowszym źródle pozostaje bez zmian.

Nie przeprowadzono nowego backtestu tej paczki ani pomiaru realnej latencji. Nie deklarujemy pełnego V3 LIVE, broker-confirmed P&L, gwarantowanego zdania challenge ani określonego miesięcznego zwrotu.

Zasady skillu risk-management wpłynęły na osobne uzbrojenie, zachowanie ochronnych cancel/flatten, wyłączność strategii i zakaz traktowania maila/modelowego fillu jako świeżego stanu brokera. Nie zastosowano jego domyślnych progów kryptowalutowych do MFF.
