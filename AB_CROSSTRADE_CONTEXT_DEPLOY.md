# A/B native route and bounded context support

This change adds `EXEC_STRATEGY_POLICY=AB_ONLY`. Original A/B can use the existing account-bound CrossTrade transport. A/B Directional, Tanja, Shallow, DOL and Continuation cannot enter through this policy. Directional detection and shadow observations remain available. The A/B adapter preserves the tick-rounded entry and protective SL, places one protected native bracket, and never infers a fill from acceptance. With AB_MANAGER_MODE=real, an A/B-specific controller applies the frozen M3 rules and Model 0; Directional execution stays in shadow. The target is frozen before alerts and Guard: 2R by default, or a 3R attempt when current AI context says ALLOW. At the first completed post-fill M1 reaching 2R, Model 0 and fresh ALLOW must both agree to retain 3R; otherwise the controller requests a market close. A market close after a completed candle is not an exact 2R fill. Existing account, session, news, drawdown, sizing, duplicate and owned-fill checks still apply.

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
AB_MANAGER_MODE=real
AB_AI_ENABLED=true
AB_AI_MAX_CALLS_PER_DAY=12
AB_AI_INTERVAL_MINUTES=60
```

Keep the existing verified Builder broker credentials and dated contract. The A/B route requires zero PRICE_OFFSET and the existing $2 MNQ multiplier / 0.25 tick. Preserve the existing ENTRY_OFFSET_PTS=1: the detector pipeline applies it before alerts, Guard and the native adapter, which retains those tick-rounded entry and SL levels. Managed targets are recalculated from the entry after that offset and its actual distance to SL. Its per-trade budget is bounded by the configured RISK_PCT and $250 Builder ceiling, with one native bracket and existing sizing costs. Shallow configuration is not used to create sibling orders in AB_ONLY. Do not enable partials, adopt an existing foreign position, or treat demo account metadata as proof of an account's financial terms.

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

Context refresh is hourly by default; 15 minutes is also supported. The worker runs NYAM 09:30–11:00 and NYPM 13:30–16:00 New York on eligible weekdays, only with fresh data and the source review calendar ready. Context expires at the session boundary, and lunch does not spend API allowance. Every source refresh checks closed M1 invalidation on the correct symbol; once invalidated, a scenario cannot revive on a price rebound. Missing/stale context is unavailable. AI never creates or vetoes the A/B entry and never independently closes a position. Its cached ALLOW is now a necessary condition for attempting 3R and retaining the runner at 2R; the local controller consumes it without additional paid calls. WAIT/VETO/expired/invalidated/missing context selects a native 2R bracket for a new signal. Model 0 selection is frozen once at 2R. SL and account Guard always take priority. Hourly context is not a substitute for M1 position management. These M3/Model 0 historical results and the six-case AI pilot used A/B Directional, not classical A/B: no PF improvement is claimed for this new combination.

The sidebar includes **AI Context · A/B**, also accessible from the A/B dashboard. The panel includes execution arm status, remaining shared allowance, both session windows and **AI Context**, with two directional scenarios, contrary evidence, liquidity reading, qualitative 2R/3R considerations, expiry/invalidation and per-candidate annotations. JSON: `/ab/ai-context`; view: `/ab/ai-context/view`.

## Verification

Broker HTTP and paid API calls are mocked in tests. The suite covers the actual A/B executor path, no TradersPost fallback, shadow strategy refusal, unchanged tick-rounded levels, native fill reconciliation, no retry after uncertain outcomes, shared daily budget, restart/concurrency, evidence validation and latched M1 invalidation. It also runs existing Builder/Guard regression tests.

Runtime review must confirm the effective policy, disabled order arm, correct bound account, broker flat/inventory state, fresh feed and the context page. Preparing or passing tests does not verify an actual broker round trip for A/B.

## Source discrepancy found during migration

The running Tanja 3.6.10 package is newer than `services/tanja` on the repository's starting commit. An environment-variable deployment can rebuild that older GitHub source. Preserve the verified 3.6.10 package and its disabled flags; do not roll back an old deployment with its formerly enabled execution variables. This PR deliberately does not replace Tanja's implementation.

## Operator handoff for brokerage activation

A/B order submission is currently disabled. Only the account operator performs the final activation. In the Builder 50K Railway service, the relevant switches are `AB_ENTRY_ARMED=1`, `AUTO_SUBMIT=1`, and `EXEC_MODE=auto`; the persisted Guard mode must also be `auto`. Keep `EXEC_STRATEGY_POLICY=AB_ONLY`, Directional shadow and Tanja execution disabled. The AI panel reports the entry switches, manager mode, calendar and actual manager decisions separately. AB_MANAGER_MODE=real prepares management but does not arm entries; AB_ENTRY_ARMED and AUTO_SUBMIT remain 0 until the operator enables trading.

The original October 19 activity warning came from a September 28 modeled trade. AB_ONLY now excludes modeled outcomes and reports incomplete broker history as unverified, never as proof of account inactivity. No test order or actual A/B broker round trip has been performed.

The main Trading desk received only the dashboard menu update on its verified `df9172f` runtime base; it links to the Builder's existing AI endpoint and starts no additional AI worker.

## Persistent deployment source

Builder must track `GaworO/AgentSignals` branch `codex/ab-crosstrade-context-20261019` until the reviewed change is merged and the source is deliberately migrated. A CLI upload alone does not change Railway’s connected GitHub branch: changing variables previously rebuilt old `main`, which rejected `AB_ONLY` and removed the AI endpoint. Pin the service source. A health-check configuration change was rejected by the Railway API and was not applied; do not assume a deployment-level endpoint gate is configured. Verify the effective source branch, commit, policy and endpoint after deployment.

## Calendar recovery and validation

The JSON export returned HTTP 429 on Railway. The loader now uses the official XML export as fallback, checks response status, structure and the current week, preserves the last successful receipt time across restarts, and backs off 15 minutes after failure. Saturday New York events may spill into Sunday UTC; the validator permits that boundary without accepting an old week's schedule. XML's UTC times were checked against the publisher's explicit ICS time zones: all five high-impact events matched in the October 18 week (86 total events). Empty, invalid, wrong-week or expired calendars cannot clear NEWS_STRICT. Existing +/-30 minute news blackout is unchanged.

## Manager validation and limits

128 focused tests passed, including native placement of the managed bracket, no action before broker fills, partial-fill refusal, improved limit fills, long/short decisions, expired/invalidation context, native M3 and runner closes, unchanged broker SL, foreign-position rejection immediately before close, unknown-result deduplication, calendar fallback and existing M1 behavior. Three frozen-model/feature parity checks passed in the expanded regression run. That run also encountered three old Directional-isolation failures; the same three failed unchanged on commit 08a7e12 before this work. No broker order or paid AI request was made by testing.

The former Win/Loss/Boundary classifier requires Directional setup metadata absent from canonical A/B, so it is not silently supplied invented features. This A/B controller uses the frozen M3 rules and Model 0 directly; its combined policy is new. It waits for confirmed full, attributed positions with verified protection and fresh completed M1. It never adopts an existing fixed-bracket order. Restarted decisions are durable; an ambiguous close is not resent. Bracket fills, partial fills and realized P&L are reconciled by CrossTrade. Actual A/B entry-to-manager-close execution remains unverified until the operator runs the strategy.
