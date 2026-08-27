# V0.1 checker agent contract

Status: candidate implementation for `V01-B02 THREE_AGENT_OPERATIONAL_TEAM`.

Starting main / branch base: `9dd4013625979492a125080f32e307fd5d808d48`.
Branch: `work/worker30/v01-b02-checker-agent`.

This slice implements only the smallest checker behavior required by the representative
V0.1 supervisor/worker/checker journey. It does not create a general reviewer authority,
Product Factory review system, orchestration framework, or new persistence layer.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE the integrated M7 `AgentHandoff` / `HandoffKind` typed result envelope and the
  existing `MultiAgentSupervisor` / `AgentRuntimePort` execution boundary.
- REUSE the existing ModelGateway for any model-assisted worker extraction that occurs
  upstream of this checker. The checker comparison itself is deterministic and therefore
  remains valid when deterministic CI has no model provider available.
- ADAPT the existing typed handoff result into one source-bound comparison summary.
- CUSTOM (thin) only the Nika-owned V0.1 semantics for source binding, agreement,
  difference, missing-result handling, and the UI-ready summary schema.

No shared M7 contract/store/supervisor file is changed in this lane because active PR #194
currently owns those surfaces.

## Input contract

The checker receives exactly two trusted source bindings:

- `source_id`: the declared source identity;
- `worker_id`: the worker assigned to that source.

It then receives zero, one, or two existing M7 `RESULT` / `ERROR` handoffs. A handoff is
accepted only when it belongs to the declared team and its sender is one of the two declared
workers. Duplicate worker output fails closed. If a worker payload repeats `source_id`, that
value must match the trusted binding; it cannot silently retarget a result to another source.

A successful `RESULT` payload may contain:

- `facts`: optional JSON object of declared facts;
- `result`: required result value for a completed worker result.

`ERROR`, an absent handoff, or a `RESULT` handoff without `result` is classified as a missing
worker result for comparison purposes. Existing error/fact evidence is retained, but the
checker does not manufacture a result value.

## Deterministic comparison

When both results exist, the checker compares:

1. the union of declared top-level fact names, in stable sorted order;
2. the complete declared `result` value.

Equal values are listed under `agreements`. Unequal values, including a fact that is present
for only one source, are listed under `differences` with each value explicitly bound to its
source. A missing side uses `present: false` and contains no fabricated `value` field.

If either worker result is missing or failed, the overall status is `missing_result` and the
checker does not claim agreement/difference for unavailable evidence.

## UI summary

`CheckerSummary.to_payload()` emits JSON-compatible schema
`nika.v01.checker_summary.v1` with:

- team/checker identity;
- stable source summaries carrying source and worker identities;
- handoff/correlation provenance when a handoff exists;
- source state: `result`, `error`, or `missing_result`;
- actual facts/result/error only when present;
- agreement fields;
- source-bound differences;
- `missing_sources`.

This is a semantic data contract for the final UI. It does not itself implement or claim the
packaged Windows/NVDA presentation.

## Runtime / model boundary

The integrated M7 supervisor remains responsible for bounded worker execution, typed worker
handoffs, failure isolation, permissions, cancellation, and restart behavior. This checker is
a deterministic comparison function suitable for invocation as the checker step through the
existing `AgentRuntimePort` path. The focused test suite includes a test-only runtime adapter
advertising the existing `DETERMINISTIC_NO_LLM` capability to prove the structural no-model
route without creating another production runtime.

If a real checker workflow needs model-assisted extraction or natural-language processing,
it must use the existing provider-neutral ModelGateway. Real Ollama/API provider qualification
is separate from this deterministic checker contract and is not claimed by these tests.

## Focused acceptance coverage

`tests/test_v01_checker_agent.py` covers:

- agreement with retained source/worker/handoff provenance;
- differences without cross-source result mixing;
- one absent worker output;
- one worker error;
- a RESULT handoff with no declared result;
- source mismatch, undeclared worker, duplicate output, and cross-team rejection;
- exactly two distinct source bindings;
- fail-closed handling of non-JSON evidence;
- structural execution through the existing deterministic no-LLM runtime contract.

Acceptance credit still requires exact-head repository CI and independent audit. This slice
alone does not prove the entire V01-B02 user journey through the final packaged UI.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`
