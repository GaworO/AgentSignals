AGENTSIGNALS v32.0 - CO ZROBIC

1. Zrob kopie wolumenu i live logow.
2. Skopiuj CALA zawartosc tej paczki do root repozytorium, zachowujac foldery.
3. Ustaw zmienne wedlug .env.v32.example; zacznij od EXEC_MODE=manual i AUTO_SUBMIT=0.
4. Skonfiguruj relay/broker callback do POST /guard/broker-event.
5. Uruchom testy opisane w V32_DEPLOY_PL.md.
6. Dopiero po poprawnym callbacku ustaw EXEC_MODE=auto; zachowaj RAMP_TRADES=10.
