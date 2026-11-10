# Broker refresh correction — 10 November 2026

Base: 204c5c6bf2aecbe0142d7cb73995b0f473a4cbb2.

A prepared Directional recovery candidate could force a full account refresh before discovering that no durable native intent existed. Repeated old candidates therefore consumed provider read capacity without any possible broker order to reconcile. Check the native journal first. Unknown/submitted intents still refresh; never infer that a missing ACK means no fill.

Reconciliation now retains authenticated raw history/margin inputs even when current rules or accounting checks fail. It re-assesses every new snapshot and never caches permission. Missing-evidence inputs are refreshed within 60 seconds; verified unchanged inputs retain the existing 300-second bound. Changed cash, fills, exposure, rules, NY date, restart, or explicit entry validation require fresh inputs. Provider errors still block and clear the cache. Rule expiry is never extended.

Entry sizing and final risk validation read complete history/costs and margin BEFORE the live account/positions/orders/fills snapshot. The snapshot's timestamp remains the actual start of those live reads. Inputs older than 30 seconds, new unmatched fills, inconsistent cash/costs and unknown margin fail closed. The existing 3,000 ms final account-age limit is unchanged; a genuinely slow inventory read still fails. Protective refreshes retain inventory-first behavior.

Background workers skip a redundant cycle when another completed inventory read is still inside the existing 15-second interval. Explicit entry and protection reads do not use that shortcut. Existing Retry-After/backoff remains durable; no quota or cooldown reset.

Validation: 558 focused tests pass, including native brackets, unknown ACKs, rejected cancels, late fills, restarts, account-wide fees, expired rules, risk sizing, ICT/IFVG handoff, recovery and emergency controls. All broker writes in tests are mocked. Regression tests cover slow history followed by genuinely fresh inventory, continued rejection at 3,081 ms snapshot age, 429 failures, and zero broker reads for 20 unsubmitted recovery candidates.

No permissions, account rules, risk percentages, AI budgets, entry/stop/target levels or advanced-management settings changed. No AI or broker order test calls authorized by this release.

Operator rollback: preserve current environment and active policy/risk files; deploy the prior exact code release only if necessary. That restores the previous read behavior and may restore these defects. It does not cancel pending orders or remove protective brackets. Do not deploy while exposure or broker state is unknown. Existing broker-native protection and persistent reconciliation journals must remain intact.
