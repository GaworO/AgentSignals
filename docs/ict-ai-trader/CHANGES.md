# Files changed for the native Shadow milestone

All application paths below are relative to `integration/agent50k-ict-trader`.
This working tree also contains the prior integration work; no commits/pushes.

| Area | Files | Result |
|---|---|---|
| Native shell | `ict_shadow.html`, `ict_shadow_cloud.py`, `ict_ai_trader.py`, `dashboard.py` | Fourth tab, shared authentication, query/bookmark compatibility, internal legacy redirect |
| Component | `services/tanja/ict_agent/static/view.html`, `native.css`, `app.js` | Scoped native UI, model states, actual sources, lifecycle history, baseline mode |
| Viewer API | `services/tanja/ict_agent/integrated_web.py`, `web.py` | Read-only native APIs, optional separate reasoning ledger, as-of history, prototype API compatibility |
| Reasoning | `services/tanja/ict_agent/reasoning.py`, `reasoning_memory.py`, `knowledge.py`, `observer.py` | Structured proposals, changed thesis, persistent lifecycle, context-sensitive retrieval |
| Pilot | `services/tanja/ict_agent/experiment.py` | Frozen cases, cost/approval binding, isolated ledger, A/B/C exports and manual scorer |
| Validation | `tests/test_ict_ai_trader.py`, `tests/test_ict_native.py`, `tests/test_ict_experiment.py`, `docs/ict-ai-trader/reports/` | 150 checks, experiment inventory and source preservation evidence |
| Packaging/docs | `.dockerignore`, `.railwayignore`, `AGENTS.md`, `STATUS.md`, `ROADMAP.md`, `DECISIONS_NEEDED.md`, `services/tanja/ict_agent/PROGRESS.md`, `docs/ict-ai-trader/` | Private assets excluded, current runbook and honest acceptance status |

`agent.py` has no net change against the checkout baseline: the existing Shadow
registration owns the extension. The prior integration adds `jsonschema` to
`requirements.txt`; no other production dependency versions were changed here.

Canonical research outputs are outside the integration checkout:
`research/ict_knowledge_20261119/corpus_615/` (new isolated notes/audit/review,
coverage, detailed rules, SQLite, RAG, master table and report) and
`research/ict_entry_reference_20261120/` (reference/atlas validation and rendered
report). No raw source was fabricated or overwritten; 2,551 old rule hashes remain.
The inline atlas is published into its project folder because the old conversation
visualization folder is not writable in this session. Old inline output is not
presented as refreshed.
