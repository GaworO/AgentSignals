# A/B native route and bounded context support

This change adds `EXEC_STRATEGY_POLICY=AB_ONLY`. Original A/B can use the existing account-bound CrossTrade transport. A/B Directional, Tanja, Shallow, DOL and Continuation cannot enter through this policy. Directional detection and shadow observations remain available. The A/B adapter preserves the existing tick-rounded Entry/SL/TP, places one protected native bracket, and never infers a fill from acceptance. Directional M3/Model0 does not manage these A/B positions. Existing account, session, news, drawdown, sizing, duplicate and owned-fill checks still apply.

Deployment is prepared with order submission DISARMED. Enabling a brokerage strategy is a separate operator action. No test orders are required or sent by this change.

## Builder 50K settings for review

```
EXEC_STRATEGY_POLICY=AB_ONLY
BUILDER_ORDER_PROVIDER=crosstrade
AB_ENTRY_ARMED=0
EXEC_MODE=manual
AUTO_SUBMIT=0
AB_V3_MODE=SHADOW
AB_DIRECTIONAL_LIVE_LONG=0
AB_DIRECTIONAL_LIVE_SHORT=0
V3_ENTRY_ARMED=0
V3_EXIT_MODE=shadow
V3_EXIT_REAL_EXECUTION=false
AB_AI_ENABLED=true
AB_AI_MAX_CALLS_PER_DAY=12
AB_AI_INTERVAL_MINUTES=60
```

Keep the existing verified Builder broker credentials and dated contract. The A/B route requires zero PRICE_OFFSET and the existing $2 MNQ multiplier / 0.25 tick. Preserve the existing ENTRY_OFFSET_PTS=1: the detector pipeline applies it before alerts, Guard and the native adapter, which retains those exact tick-rounded signal levels. Its per-trade budget is bounded by the configured RISK_PCT and $250 Builder ceiling, with one native bracket and existing sizing costs. Shallow configuration is not used to create sibling orders in AB_ONLY. Do not enable partials, adopt an existing foreign position, or treat demo account metadata as proof of an account's financial terms.

Use Railway reference variables (never paste credentials into source):

```
OPENAI_API_KEY=${{tanja.OPENAI_API_KEY}}
OPENAI_MODEL=${{tanja.OPENAI_MODEL}}
AB_CONTEXT_SOURCE_PASSWORD=${{tanja.TANJA_DASHBOARD_PASSWORD}}
```

Tanja remains the data/context source, with:

```
TANJA_AI_ENABLED=false
TANJA_CROSSTRADE_ORDERS_ENABLED=false
TANJA_TRADING_LOOP_MODE=off
```

The worker reads the authenticated Tanja market packet, validates its hash, closed timestamps, ES/NQ/MNQ continuity and HTF coverage, then sends only market evidence to OpenAI. Tanja account state, credentials, CSRF, past decisions and trade outcomes are excluded. Twelve is a hard maximum of attempts per New York calendar day, not a promise of prepaid monetary credits or twelve required calls. Calls already spent by Tanja that day count against the same allowance. A running or transport-unknown request blocks further calls that day, including after restart. SQLite reservations serialize multiple workers.

Context refresh is hourly by default; 15 minutes is also supported. The worker runs 09:00–16:00 New York on eligible weekdays, only with fresh data and the source review calendar ready. Every source refresh checks closed M1 invalidation on the correct symbol; once invalidated, a scenario cannot revive on a price rebound. Missing/stale context is unavailable. AI is advisory: it neither vetoes an order nor changes targets or closes a position. This preserves the distinction between diagnosing a recommendation and claiming an improvement in PF.

The A/B dashboard includes **AI Context**, with two directional scenarios, contrary evidence, liquidity reading, qualitative 2R/3R considerations, expiry/invalidation and per-candidate annotations. JSON: `/ab/ai-context`; view: `/ab/ai-context/view`.

## Verification

Broker HTTP and paid API calls are mocked in tests. The suite covers the actual A/B executor path, no TradersPost fallback, shadow strategy refusal, unchanged tick-rounded levels, native fill reconciliation, no retry after uncertain outcomes, shared daily budget, restart/concurrency, evidence validation and latched M1 invalidation. It also runs existing Builder/Guard regression tests.

Runtime review must confirm the effective policy, disabled order arm, correct bound account, broker flat/inventory state, fresh feed and the context page. Preparing or passing tests does not verify an actual broker round trip for A/B.

## Source discrepancy found during migration

The running Tanja 3.6.10 package is newer than `services/tanja` on the repository's starting commit. An environment-variable deployment can rebuild that older GitHub source. Preserve the verified 3.6.10 package and its disabled flags; do not roll back an old deployment with its formerly enabled execution variables. This PR deliberately does not replace Tanja's implementation.
