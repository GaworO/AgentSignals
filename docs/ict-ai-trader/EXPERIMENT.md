# Frozen first LLM/RAG experiment — PREPARED_NOT_SENT

Experiment: `6cfdb0f52d6cec4d0844`.
Private plan: `runtime-assets/experiments/6cfdb0f52d6cec4d0844/plan.json`.
Plan SHA-256: `b2302eb7f73d65e032de1040d983b073f8bb530c4d8be6db33a9d06e10730669`.
Public inventory: `reports/pilot-experiment.json`; its source path is redacted and
it cannot replace the private approval-bound plan.

15 real May 2026 snapshots: three research candidates, three incomplete, three
invalidated, three ambiguous execution situations, three NO_TRADE. Candidate
validity is NOT established by the detector label; no source-certified positive
ground truth has been invented. Independent market/source adjudication is needed.
Later audit outcomes are used only to stratify sampling, never in model packets.
This archive was already used in research and is not an untouched holdout.

A reuses the exact recorded deterministic decisions at zero provider cost.
B/C use identical as-of market anchors and no prior model answer. B has no source
records; C includes 9–11 verified source records and explicitly scoped audit notes.
C uses the pinned 135-video / 2,551-record release `f11b1442ab01a829038b`.
The subsequent 136th video does not silently change this frozen experiment.

## Model, costs and permission

- OpenAI `gpt-5.4-mini-2026-03-17`, medium reasoning, standard synchronous Responses.
- 30 calls, maximum 12,000 output tokens each, 90-second timeout, no automatic retry.
- Input token upper bound: serialized UTF-8 request bytes + 4,096 cushion.
- At standard USD rates $0.75/M input and $4.50/M output: approximate cost **$2.31**
  assuming bytes/3.5 input tokens and 4,000 output tokens per call. Not a tokenizer estimate.
- Sum of conservative reservations: **$7.913328**; suggested total cap **$8**,
  before tax, no batch/cache discounts. Recheck rates immediately before approval.
- Actual paid calls: **0**, model answers: **0**, measured quality/latency: unavailable.

Official model/rates: https://developers.openai.com/api/docs/models/gpt-5.4-mini
Structured output: https://developers.openai.com/api/docs/guides/structured-outputs

`approval-template.json` is intentionally `approved:false`. Execution requires a
matching plan hash, exact model, approval ID, UTC expiry, max_calls=30, max_usd=8
and a dedicated `ICT_AI_API_KEY` already provided through the approved environment.
Never paste the key into a command, document, browser or repository. The existing
production key/budget grants no permission. The user chose to keep API OFF.

After explicit approval only, from the integration checkout:

```sh
python3 -m services.tanja.ict_agent.experiment run \
 --assets runtime-assets/knowledge/f11b1442ab01a829038b \
 --plan runtime-assets/experiments/6cfdb0f52d6cec4d0844/plan.json \
 --approval /private/approved-ict-pilot.json
```

Before spending, all frozen packet hashes, model/prompt/schema and retrieval
versions are verified against current immutable source snapshots. There is both
an experiment-wide and daily budget cap. Unknown submission outcomes retain the
reservation and halt continuation; reconcile manually, never resubmit blindly.
An interrupted run resumes completed rows without duplicate calls.

## Evaluation

`export_blind` exports all three arms, randomized opaque item IDs and a separate
private arm key. Citations and output structure can reveal an arm, so blinding is
partial. Mechanical metrics record decisions, validation state, latency and
provider token-accounting cost ceilings. A has zero API cost; its replay runtime
is not available per decision and is not fabricated.

Human raters score market interpretation, missing conditions, invalidation,
scenario consistency, NO_TRADE quality and source grounding (0/1/2/null). They count
unsupported factual claims, check exact cited source passages and explain ratings.
Every item must be rated or explicitly null; unratable is not a success. A source
specialist separately audits grounding. No semantic reference labels exist yet.
Use `score --plan .../plan.json --ratings .../ratings.json` after export to produce
per-arm means, rated/unratable counts and paired C-minus-B quality differences.
Unpaid fixtures do not contribute to these results.

This pilot measures reasoning quality, not profitability. Independent snapshots
cannot establish long-run consistency, generalization or execution performance.
The 37 ambiguous fills remain unresolved and never become profitable results by
agreement with a model response.
