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

## 2026-11-16 persisted event-sequence correction

Read-only audit of integrated Section 2 source at `2f92a120` found a concrete
boundary failure: the coordinator wrote `sequence` before expanding the
adapter's payload, so a payload key could replace the validated event sequence
in SQLite. Reproduction through `TaskRuntimeCoordinator.start` and a reopened
store preserved forged `-99` despite the canonical event sequence being `7`.

The same coordinator now writes the canonical sequence after copying ordinary
payload fields. This preserves the existing flat audit schema and all other
payload data, without introducing another validator or authority. Six regression
cases exercise the ordinary payload and five collisions (negative, overflow,
string, null and boolean), initial waiting-for-approval, resume using a fresh
store/coordinator, and final audit readback from another store. The path includes
Ukrainian text and spaces. Before repair, the five collision cases failed; the
ordinary case passed. Independent review found no blocking issue and passed
26 runtime/coordinator/recovery tests plus Ruff.

The earlier audit ran 286 existing Section 2/runtime/product-command tests
successfully on `2f92a120`; their historic "authored only" entries above describe
the original changes, not the current test state. This repair still requires
its own exact-head full checks and main integration. Fresh objects in one process
prove persistence/recovery behavior here; they do not claim a physical crash,
NVDA, live cloud or whole-product acceptance. Reuse canonical PR #1856 after
its already integrated ancestry, with Section 1 closure preserved.


## Canonical adapter-result snapshot convergence

Independent read-only audit of candidate `4cb2fe03ee125de9eb9afe65edcf459ac1b423d5`
completed the repository-controlled Section 2 reuse/evidence map: 386 focused
runtime, recovery, product-command and plugin tests passed. That candidate also
passed full hosted Core and M12 (`43908417478`, `43908417459`). Existing
framework-neutral ports, canonical identities, versioned product/plugin
projections and recovery authorities remain the implementation baseline.

The audit reproduced one concrete remaining gap: an otherwise valid runtime
event containing `{"value": object()}` passed DTO admission, then failed JSON
serialization during audit finalization. Reopening SQLite left the task RUNNING
with only runtime.started. This is an adapter-result boundary defect, not a new
store or permission system.

Reuse the existing `canonical_runtime_result` and JSON snapshot from PR #916 /
#917 (exact source `142504f9f4b6c5ce8f8683d7461ca3236ba43e53`), adapted into the
current contracts. Preserve every current DTO identity/budget/sequence guard,
validate nested keys/cycles before serialization, allow readonly Mapping
carriers through a Mapping-only JSON default, reject nonfinite/non-JSON/invalid
Unicode leaves and reconstruct through current DTO constructors. The detached
snapshot prevents later adapter mutations changing accepted result evidence.
The accepted payload sequence collision remains governed by the coordinator's
canonical-key-last rule, rather than the older branch's blanket rejection.

Run/resume canonicalization occurs inside the existing adapter exception
boundary, producing generic FAILED/INTERNAL results which can finalize durably
without leaking adapter exception text. The same reused lineage requires an
exact boolean cancellation acknowledgement inside the current cancellation try:
malformed acknowledgement retains UNCERTAIN ledger state, the active session and
the existing replay block. Ledger transactions and recovery owners are retained.

Regression factories forge constructor-invalid fields explicitly so malformed
values reach the adapter boundary; tests do not falsely pass by failing before
adapter invocation. Run and saved-resume cases reopen SQLite, inspect terminal
state/audit, and verify completed recovery claims; cancellation cases preserve
uncertainty and prevent replay. Nested readonly/shared-subtree positives and
detached Ukrainian output remain supported.

The local environment became unavailable after the earlier 95 focused tests
passed and before the final additions/lint correction could execute. No local
PASS is claimed for this reconstructed patch. Fresh hosted Core Ubuntu/Windows,
complete M12, independent changed-source review and integration/readback are
required before Section 2 terminal closure. Source reuse decisions do not award
human NVDA, physical crash or real cloud acceptance.


## Verified terminal closure — 2026-11-16

**Independent 2А / 2Б / 2В and aggregate Section 2: DONE.** Exact candidate
`17bbeecc2fd9266bc39e004470cd3855f8faf7e8` passed Core Ubuntu/Windows and
complete M12, including the extracted final Windows ZIP proof. Independent
review preserved every incumbent validator and confirmed the reused snapshot/
cancellation boundaries. Linux: 3003 passed / 5 skipped; M12 Windows: 3001
passed / 7 skipped. Canonical #1856 integration `7ea95d888e17a9fe4aff847404f8840c1ed7acea`
retained the qualified tree `4e28289e664fb5e182ecc89bcd22512f8084868b` exactly.
Run/job/artifact/hash details are in MULTI_PLAN_CLOSURE_STATE.md. Earlier
nonterminal statements document historical checkpoints; they do not reset this
qualified closure. Human NVDA and whole-product/real-cloud acceptance remain
separate and unclaimed.
