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


4. Runtime event `sequence` now fails closed above the signed-portable JSON
   integer ceiling `(2**53) - 1`, preventing lossy browser/Web projection and
   oversized SQLite integer binding. The existing nonnegative, exact-`int`
   contract still applies; the boundary value remains valid. New negative
   tests cover 2**53, 2**63 and very large integers. These fixtures have not
   yet been executed by exact-head CI.

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

## Nested JSON-object identity and bounded inspection (nonterminal)

The existing `RuntimeRequest`, `RuntimeEvent` and `RuntimeResult` mapping
validator now inspects maps nested through mapping/list/tuple carriers, rather
than checking only the top-level keys. It rejects plain-int, boolean or other
non-string nested keys which would silently alias during JSON projection.
An iterative bounded depth-first traversal also rejects cyclic containers
without Python recursion, while permitting reused acyclic subtrees and
read-only mapping proxies. An explicit 20,000-node inspection budget bounds
resource consumption before runtime dispatch; it is not a general serializer,
deep value-schema validator or lifetime job quota.

Negative/compatibility tests cover nested key collisions, cycles, shared
subtrees and oversized carriers. The broader versioned command/query,
recovery, restart and all-client interoperability contract remains unproved
until exact-head CI and integration evidence; **Plan 1 Section 2 NOT DONE**.
