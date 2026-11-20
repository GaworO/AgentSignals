# Acceptance evidence — native Shadow, 20 November 2026

The local implementation milestone is complete. Empirical trading intelligence,
full-corpus coverage and production readiness are not declared complete.

| # | Criterion | Evidence | Limit |
|---|---|---|---|
| 1 | Native tab inside ICT Shadow | Fourth tab in `ict_shadow.html`, `view=ictai`; real browser check | Local only |
| 2 | No second app | Native fragment and scoped CSS/JS; zero iframe elements | Loopback harness is for review, not production entrypoint |
| 3 | Existing functions preserved | 150 tests include prior Shadow/history/scanner suites; browser navigation | Production workers were not cold-started |
| 4 | Historical URLs | Auth/detail routes retained; query parameters preserved; saved review auto-open/fallback | No production review collection copied into preview |
| 5 | Actual data | Frozen NQ/MNQ/ES replay, six chart panels, exact candidate snapshot | Live feed absent in preview |
| 6 | Actual LLM distinct | Separate answer panel and five normalized states; NOT_CALLED now | No paid model answers yet |
| 7 | RAG before evaluation | Contextual LONG/SHORT/exception retrieval enters `reasoning.prepare` | Empirical improvement unmeasured |
| 8 | Traceable sources | Canonical 3,824 cue references; atlas source checks PASS | Text only, not verified audio/chart |
| 9 | Persistent hypotheses | Stable IDs, immutable transitions, prior validated answer; restart/as-of tests | Actual longitudinal model quality unmeasured |
| 10 | Timestamped evidence | Recursive as-of anchors, typed exact numeric bindings | Archive receipt latency assumed; semantic truth requires audit |
| 11 | Controlled experiment | Frozen 15 cases, A/B/C, 30 B/C packets, approval-bound runner, cost cap | Semantic labels and certified positive cases unestablished |
| 12 | Execution uncertainty | Existing full 37-case audit retained, conditional path bounds | All 37 remain unresolved |
| 13 | API cost controls | Dated authorization, global experiment and daily caps, usage ledger | Recheck rates before approval; estimates before tax |
| 14 | Relevant tests pass | `reports/tests.xml`: 150 passed in 91.13s; JS syntax checks | Software checks do not prove profitability |
| 15 | Railway prerequisites | Updated deployment runbook, excluded private build context, additive tables | Container/current SHA/volume/resources remain to verify |
| 16 | New live trading disabled | No broker transport, default OFF, disabled Live mode, risk separation | Existing unrelated production settings were not changed |
| 17 | No unauthorized paid/deploy action | Zero new provider calls; no push/deploy/credential/risk change | Later stages need explicit authorization |

## Open locally

http://127.0.0.1:8877/ict-shadow?view=ictai&ai_snapshot=07d11f31ef98debe183a8bef

The candidate is a stored research proposal from May 1, 2026, 10:31 New York, not
a confirmed trade. Compare NQ/MNQ/ES and individual timeframes or all six. Inspect
liquidity origin and source timestamps. The main decision remains deterministic;
the separate model panel states NOT_CALLED. Browser mode selection starts no calls.
History/scanner/live tabs remain in the same shell and retain their own parameters.

## Artifacts

- `reports/tests.xml`: full targeted integration/regression suite.
- `reports/pilot-experiment.json` and `EXPERIMENT.md`: frozen experiment and budget.
- `REASONING-AUDIT.md`: eight capability answers with code references.
- `reports/corpus-increment.json`: 7 new rules, previous 2,551 hashes preserved.
- `reports/corpus-remaining.json`: 136 full / 479 remaining catalogue entries.
- `reports/ambiguities.json`: previous verified 37-case investigation, unchanged.
- `DEPLOYMENT.md`: exact app mount, storage, authentication and rollout prerequisites.
- `CHANGES.md`: changed implementation files and canonical research outputs.

The canonical entry atlas passed 95 checks with 2,558 records / 3,824 references;
source visual/audio and complete cross-video conflict checks remain false. The
pilot pins the prior 135-video release intentionally. Local optional pandas
accelerators emit NumPy ABI warnings without failing the tested routes; Python
3.12 image and true cold-start checks remain a production prerequisite.
