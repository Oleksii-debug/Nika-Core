# Plan 1 — Section 2: stable runtime identity and budget contract hardening

Scope: **only Plan 1, Section 2**. Reuse existing Nika-owned DTOs and ports
rather than introducing a second runtime, provider, persistence layer or UI
command authority.

## Reused architecture
- `src/nika_core/runtime/contracts.py` remains a framework-neutral Protocol
  boundary shared by reference and LangGraph adapters.
- `src/nika_core/runtime/coordinator.py` controls durable request/resume
  orchestration; `src/nika_core/runtime/recovery.py` and `langgraph_runtime.py`
  perform checkpoint preflight.
- `src/nika_core/product_command/contracts.py` already exposes Pydantic
  JSON-serializable application projection contracts that Windows/Web adapters
  can consume without importing the Windows host into Nika domain.
- Reuse existing `tests/test_runtime_contracts.py`,
  `tests/test_runtime_startup_recovery.py`,
  `tests/test_product_command_contracts.py` and their integration tests.

## Repaired invariants
1. Both initial and restart/resume envelopes accept only positive **integer**
   step counts; booleans and floats cannot masquerade as quotas.
2. Timeout budget, when present, must be numeric, positive and **finite**;
   `NaN`, infinities, booleans, strings and overflow fail closed *before*
   any runtime side effect. Both paths call the same validator.
3. Recovery approval and checkpoint authority require actual Nika-owned
   `RuntimeResumeMode` / `RuntimeResumeProbeStatus` enum instances. A
   string equal to `"ready"` must never claim a verified checkpoint.
4. Event ordering requires real nonnegative integer sequence numbers.

## Negative/recovery/integration evidence
`tests/test_plan1_runtime_contract_hardening.py` covers the invalid
budgets, forged `ready` state, invalid resume mode, event sequence and
valid reference-runtime execution. Existing recovery and runtime tests remain
binding. This is component-level evidence, **not** physical Windows/NVDA,
live Web/Cloud or packaged-product acceptance.

At the first change, branch ancestry includes Plan 1 Section 1 candidate
`f29d26cd179499e5ac3406b4291bf67203d7ecc2`; exact Section 2
head/CI/integration evidence must be checked before claiming terminal DONE.

## Follow-up identity admission repair
Durable run/task/thread/resume/checkpoint identities are now validated as real Python strings, NFC-normalized, nonblank, without surrounding whitespace, Unicode control/format/surrogate characters or embedded line separators before they can become runtime or recovery authority. Additional negative regression cases cover type confusion, Unicode bidi/zero-width, and forged multiline identifiers. Existing runtime adapter and persistent recovery paths stay unchanged; exact-head CI and integration readback remain mandatory before DONE.
