# Reasoning audit — native Shadow release, 20 November 2026

The implemented LLM path is callable after authorization. Actual provider state is
NOT_CALLED: no genuine answer or measured RAG improvement exists yet. Fixture
responses in tests are explicitly labelled INJECTED_TEST_TRANSPORT.

| Question | Actual implementation and boundary |
|---|---|
| What is deterministic? | `market.py:advance`, `observer.py`, `setups.py`, `decision.py`, `risk.py` and `simulator.py` derive bars, structures, candidate states, eligibility and simulated execution. The UI's main action/commentary currently comes from these stored snapshots. |
| What comes from a real LLM? | After approval, `reasoning.py:review` invokes `openai_transport` through the existing `directional_ai_transport`. Only its separately stored answer is displayed as model reasoning. No such call has occurred. |
| Which decisions are hardcoded? | `decision.py` produces the research baseline. Thresholds and source templates are engineering policies, not inferred professional intuition. The new LLM answer may disagree with that baseline, but cannot grant execution permission. |
| Can RAG affect evaluation? | `Knowledge.contextual` independently searches LONG, SHORT and exceptions/no-trade before `reasoning.prepare` constructs the prompt. Selected rule IDs and supporting/opposing references are required in structured assessments. Empirical influence remains unmeasured until B/C runs. |
| Is the database only explanation? | In the deterministic baseline, references do not make the engine discretionary. In the new provider path, source conditions, examples and caveats enter the decision prompt itself, before its proposed decision. |
| Are contradictory sources considered? | Retrieval has an exception lane; records preserve exceptions, ambiguities and contradictions. Responses have supporting/opposing rule IDs. Retrieval roles are intentions, not a semantic proof of contradiction. Semantic correctness still requires human rating. |
| Is memory persistent? | `reasoning_memory.py` stores stable hypothesis IDs, full assessments and immutable from/to transitions. `prepare` supplies the previous validated answer. Rejected responses do not update memory. Nonchronological updates, duplicate requests and concurrent unresolved reviews are blocked. |
| Is information available as of the decision? | `ai.packet` recursively resolves cited anchors and rejects future timestamps. Chart APIs clip by snapshot as-of. Prior answers are constrained by market time; live answers also by received time. Archive bars assume availability at close; original collection latency is unknown. |

## What is newly implemented

Schema v3 includes entry logic, missing confirmations, next event, lifecycle,
changed observations and whether the previous thesis survived. Every contextual
LONG/SHORT/NEUTRAL hypothesis and detected setup is assessed. The model can choose
an interpretation before a deterministic entry is detected. It cannot invent a
new executable family or turn an advisory into an order.

Numeric claims bind evidence ID, exact field and value, matching symbol and
contract. Unknown sources, future anchors, incompatible proposal levels, stale
answers and unsupported state transitions are rejected. Text is prohibited from
containing digits; this cannot detect every unsupported verbal assertion or
numbers written as words. A valid reference does not prove semantic entailment.
The rubric explicitly audits that gap. No claimed guarantee of zero hallucination.

The native panel maps missing/IN_FLIGHT/VALIDATED_ADVISORY/transport-unknown/rejected
records to NOT_CALLED/RUNNING/COMPLETED/FAILED/VALIDATION_REJECTED. It exposes actual
origin, sources, changes and lifecycle history. The underlying market journal is
read-only in the pilot; paid accounting and answers use a separate ledger.

## Retrieval and longitudinal limits

Local 64-dimensional LSA plus lexical matching is not a pretrained semantic
embedding model. Two source-bound ordered concept templates exist; other records
are not compiled strategies. Bidirectional retrieval uses current ranges, active
zones, liquidity origins and observed event sequence. It cannot establish that a
rule's market conditions are fulfilled merely by retrieving it.

All pilot packets retain every referenced as-of market anchor and all 9–11 selected
source records. Each explicitly identifies one omitted large cross-video audit
(`audit-coordinator-qc-film400`) by ID/hash. Complete exceptions inside retained
source records remain. The omitted document remains in the corpus; no claim of
complete cross-video conflict coverage is made. Packet construction fails if
market context cannot fit; it never silently truncates market anchors.

The pilot deliberately uses independent snapshots without prior model answers to
isolate retrieval. Longitudinal state transitions are tested with fixtures; a
later chronological paid run is still needed to evaluate real model consistency.
No forward paper or profit validation has been completed.
