# Weryfikacja korekty Builder 50K — 29.09.2026

Podstawa: paczka ENTRY-ONLY v31.24 oparta na ZIP użytkowniczki
AgentSignals-main - 2026-09-29T130249.951.zip
(SHA256 2f7f813a16f08bf96139bd6d08a4e14e1ffcf4e1d4a1f6c0e51493905716509c).

20 nowych testów korekty + 140 wykonań wybranych wcześniejszych testów = 160 PASS.
Osobno 3 testy Directional engine w izolowanej przestrzeni continuation_runtime: PASS.
Łącznie 163 wykonania, bez błędów/asercji w tych finalnych uruchomieniach.
To nie pełny test wszystkich plików repo ani brokerski test end-to-end.

Nowe testy obejmują:
- akceptację rosnącego floor i odrzucenie wartości w skali 100K/NaN/Inf;
- zachowanie kill, ramp, streak, mode, całej historii oraz kopii przed zmianą;
- odmowę przy AUTO, uzbrojeniu, złym planie/etapie, weak token, innym route;
- odmowę bez ręcznych potwierdzeń, przy pending/open commitments i uszkodzonym stanie;
- brak obniżenia wcześniej zweryfikowanego floor;
- błąd kopii bez dotknięcia stanu;
- wymaganie POST i autoryzacji endpointu oraz brak uzbrojenia po korekcie.

Python compile guardrails.py i składnia JavaScript formularza (node --check): PASS.
Testy sieciowe wykonawcy używały mocków. Nie wysłano realnych zleceń ani API requestów do brokera.

Testy Directonal nie mogą być mieszane z root detcore w jednym interpreterze;
uruchomiono je osobno z preimportem pakietu continuation_runtime.detcore tak jak
izolowany detektor. Pierwsza wspólna próba miała błąd wykrywania tego modułu; poprawiono
środowisko testu, nie kod strategii. Lokalny interpreter ma Flask po dołączeniu biblioteki
zewnętrznej; próby tła skanu w mieszanym środowisku pokazały ograniczenia importów
Flask/pathlib w procesie potomnym. Nie jest to dowód poprawnego skanu end-to-end w Railway.

Istniejący importer historii z ostatniego ZIP nie ma kontroli daty dopasowania,
więc w tej paczce nie wykonano i nie zalecono masowego uzgadniania broker fills.
Seria 4 grup modelowych vs 3 ostatnie pozycje brokerskie pozostaje do wyjaśnienia.
Nie uznano wpisów nieobecnych na zrzucie za niewykonane i nie zmieniono ich outcome.

Zasady skill risk-management wpłynęły na wymóg MANUAL/disarmed, zachowanie blokad,
kopię stanu i odmowę obniżania zweryfikowanego floor. Nie zastosowano kryptowalutowych
limitów procentowych ani nowych filtrów strategii.

Pełny manager V3 nadal zablokowany. Nadal brak ciągłego automatycznego broker feedback.
Nie zmieniono detektora, jego source locks ani wyników backtestów.
