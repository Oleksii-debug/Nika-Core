# Plan 1 / Section 2 — bounded runtime-port ingress repair

Scope: only Nika Core Plan 1 Section 2, child of existing Section 2 PR #1738.
The canonical `AgentRuntimePort`, `RuntimeRequest`, `RuntimeResumeRequest`,
`RuntimeResumeProbe`, `RuntimeResult`, `RuntimeEvent` and existing
reference/LangGraph adapters remain in place. No new runtime, identity registry,
persistence engine or transport DTO hierarchy is introduced.

## Fixed production contract gaps

1. Runtime task/thread/checkpoint/resume identifiers were Unicode-canonicalized
   but **unbounded**. The existing canonical validator now caps encoded UTF-8
   identifiers at 512 bytes before downstream runtime/persistence admission.
   This applies to initial request, recovery request, probe, resumable result and
   event type. Limit is defined once as `MAX_RUNTIME_ID_UTF8_BYTES`.
2. Previously, custom numeric `float`/`int` subclasses could enter timeout
   policy and execute overridden comparison/conversion operations at the
   application boundary. Only exact built-in `int`/`float` carriers now pass,
   with existing positive finite checks retained.
3. Event type names reuse the same normalized, control-free identity validation
   instead of merely checking nonempty text, preventing multiline/bidi event
   name ambiguity.

## Regression tests

`tests/test_plan1_runtime_identity_bounds.py` exercises each of the five
admission/egress DTOs with oversized ASCII/multibyte identifiers and hostile
control/format characters. It proves bounded 512-byte canonical positives and
rejects polymorphic timeout carriers without invoking their comparisons.
Inherited `tests/test_plan1_runtime_contract_hardening.py` and
`tests/test_runtime_contracts.py` remain applicable.

These are authored tests, **not executed PASS claims**. Exact-head Ubuntu and
Windows Core CI, integration/readback and broader Section 2 command/query/event
versioning, canonical cross-domain identity/fencing, and all-client application
service interoperability are still to be qualified.

This PR is **NOT terminal DONE**. Do not claim product/physical/NVDA evidence,
or update GitHub/Drive closure status, until the full Section 2 criteria pass.
