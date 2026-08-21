# PF4 Product Factory deep acceptance matrix — 2026-08-20 / reconciled 2026-08-21

## Purpose and ownership

PF4 is the independent Product Factory acceptance lane. This batch deliberately owns executable QA,
compatibility evidence, exact-candidate lineage and release-evidence checks. It does **not** repair
PF1/PF2/PF3/PF5 production behavior, manual DEV01–DEV05/M10 source, DesktopBackend/WebView/UIA,
ModelGateway internals or credential/provider implementations.

The batch began on main `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff` and was repeatedly refreshed as
Product Factory work integrated. The current compatibility baseline for the reconciled matrix is
`449ed6dc34e8911aa2759b2a1219fd2720d11dd8`.

Acceptance policy:

- a red adversarial test is useful evidence when integrated production behavior violates an invariant;
- production-owner failures are not marked `xfail`, skipped or weakened to manufacture green CI;
- PF4-owned test mistakes are corrected when discovered and are not blamed on product owners;
- old exact-head results become lineage evidence after the branch or main moves;
- merge-ref success does not replace exact candidate-head evidence;
- cancelled checks are not green checks;
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false` until the packaged natural-language Product Factory journey is executable.

## Quantitative acceptance delta

The matrix has now passed through three materially different evidence states:

1. Initial deep matrix: **770 passed / 28 failed**.
2. Current-main refresh after integrated PF1/PF3 repairs: **1018 passed / 17 failed**.
3. PF4 self-audited current-main matrix: **1021 passed / 14 failed** on Ubuntu exact-head verification,
   with dependency consistency, Ruff and compile all passing before pytest reached the intentional
   product acceptance failures.

The drop from 28 to 14 must not be interpreted as fourteen arbitrary tests being removed. It has
three causes with separate truth accounting:

- **11 former product defects are independently proven repaired** by the same adversarial assertions;
- **2 PF4 assertions were false positives** and were corrected to the real public-contract boundary;
- **1 PF4-owned release-evidence defect** (final distributable ZIP SHA-256 lineage) was repaired in
  this QA/workflow batch and its regression now passes.

This distinction is required so PF4 does not inflate product progress by deleting valid failures or
inflate blocker counts by keeping invalid tests.

## Eleven former product failures now independently closed

The current-main matrix now passes the same attacks for these previously failing families:

1. PF6 current-release state is scoped by ProductProject and environment, so equal environment text
   in two products no longer transfers the first product's previous release SHA to the second.
2. PF6 rollback no longer adopts another ProductProject's previous release.
3. PF7 raw token-shaped scalar values cannot hide under an innocent durable ProductProject key.
4. PF7 raw token material cannot masquerade as a ProductProject credential reference.
5. PF1 Research handoff rejects a ProductOption that references an evidence package never recorded.
6. PF1 ProductRequirement identity is unique inside a ProductProject specification.
7. PF5/PF3 execution restore rejects two active leases on one execution node.
8. PF5/PF3 execution restore rejects a lease whose expiry precedes issuance.
9. PF6 environment/provider identity rejects empty values.
10. PF6 successful rollback evidence must restore the exact previously recorded release SHA.
11. PF6 restored current-release state must be backed by coherent snapshot deployment evidence.

These are integration credits for the relevant foundations, not complete PF-gate closure.

## PF4 self-audit corrections

### Correction A — ProductProject freshness belongs at the required rebind boundary

The original attack created `ProductProjectCoordinatorBinding`, checkpointed a coordinator, mutated
SQLite through `ProductProjectRepository`, then reused the **same stale binding object** and required
that object to discover the mutation itself.

That was an invalid requirement. The binding is intentionally constructed from a particular
`ProductProject` value. The checkpoint host is the recovery boundary that must re-read the current
ProductProject and re-bind before resume.

The corrected test therefore:

1. creates and checkpoints against ProductProject version N;
2. durably mutates the ProductProject to version N+1;
3. reads the current ProductProject after the restart boundary;
4. constructs the required new binding;
5. requires `StaleProductProjectBindingError` when restoring the old checkpoint.

Current main passes this corrected contract. PF4 withdraws the earlier demand that the binding itself
perform an implicit repository read.

### Correction B — credential restart must model an actual process/store boundary

The original PF7 fixture created a second CredentialBroker over the **same in-memory fake protected
store object** and required broker restore to delete old store handles. That is not a real process
restart.

The integrated Windows protected-store adapter intentionally persists OS-backed secret material but
keeps opaque handle state process-local. The corrected cross-layer test therefore uses one fake
WinVault backend, constructs a first `WindowsCredentialStore` + broker, issues a lease, then creates a
**new WindowsCredentialStore instance** over the same backend and a new broker. After restore:

- the old broker lease cannot be authorized;
- the old process-local handle cannot be validated by the new store;
- OS-backed material may still exist for legitimate post-restart re-issuance.

Current main passes this corrected test. PF4 withdraws the old same-object fake-store blocker.

## Current exact remaining acceptance failures — 14

### PF2 coordinator semantic restore / PF12 — 4

Current coordinator restore still accepts semantically forged state that has structurally valid
serialization:

1. `ACCEPTED` work without worker result or independent accepted review;
2. nested request repository identity drift;
3. nested request ProductProject identity drift;
4. restored allowed-path or permission expansion.

A checksum or structurally valid snapshot is not sufficient semantic proof. Restored records must be
revalidated against the canonical graph/request/review invariants before dependency advancement.

Owner: PF2 coordinator/recovery.

### PF2 implementation evidence completeness / PF4 — 2

A component can declare multiple `acceptance_commands`, yet a worker result containing only one easy
passing command can still reach `REVIEW_REQUIRED`. An unrelated passing command can also substitute
for the declared matrix.

Required invariant: every declared acceptance command has exact corresponding successful evidence;
missing or substituted evidence cannot create a successful implementation candidate. Independent
review remains required after complete worker evidence and must not be bypassed.

Owner: PF2 coding-worker adapter/coordinator reconciliation.

### PF2/PF3 team/repository integrity — 5

The current orchestration graph still permits these attacks:

1. the same physical provider/locator repository represented by multiple logical repository IDs;
2. an integration decision that repeats the candidate lease and omits the actual conflicting owner;
3. a dynamic specialist assigned to a phantom component not present in the composed project;
4. an empty opaque repository credential identity such as `credref:`;
5. an empty ProductComponent identity.

These are identity/ownership validation failures, not scale failures.

Owner: PF2/PF3 repository/team orchestration.

### PF7 credential audit monotonicity — 1

`CredentialBroker.restore()` validates duplicate persisted audit IDs but accepts `next_event` behind
already persisted event identities, then assigns that counter directly. A tampered/rolled-back
snapshot can therefore make a future operation reuse a previously durable audit event identity.

Required invariant: restored counters must be strictly beyond persisted identities (and analogous
lease counters must not allow durable identity reuse when such identity is persisted/authoritative).

Owner: PF7 credential broker.

### PF10 release/license verification — 1

`build_third_party_notices()` has meaningful license collection, but
`verify_third_party_notices()` currently verifies only that the notices file contains “Python runtime”
and every runtime distribution name. A deliberately names-only file passes despite containing no
license declaration/text evidence.

Required invariant: the verifier must prove the generated license-evidence structure, not package
name presence alone, and release policy must remain capable of failing closed on unacceptable or
missing license evidence.

Owner: release/compliance packaging surface.

### PF11 packaged Product Factory composition — 1

The packaged Windows composition root still routes ordinary task creation directly through
`DesktopBackend.create_task`; it does not compose `ProductProjectCommandService` / Product Factory
command routing into the packaged command journey.

A backend Product Factory and a separate Command Center are not PF11 until the packaged user command
actually reaches the durable ProductProject/Factory route.

Owner: PF5 packaged Product Factory composition. Shared UI/UIA remains separately owned by the
Interaction/M5 lane.

## PF4-owned release evidence repair — final ZIP identity

The original M12 workflow created the outer distributable ZIP after the internal release manifest and
then wrote `m12-prehuman-evidence.json` without identifying that final uploaded ZIP by digest.

PF4 repairs only this QA/evidence-lineage boundary:

1. create `NikaCore-<version>-windows-x64.zip`;
2. require the exact path to exist as a file;
3. compute `Get-FileHash -Algorithm SHA256` after compression;
4. normalize the result and require exactly 64 hexadecimal characters;
5. expose exact ZIP path + digest through the workflow step outputs;
6. write both `distributable_zip_path` and `distributable_zip_sha256` into pre-human evidence;
7. upload the same exact ZIP path after evidence is recorded.

The PF4 regression requires the semantic order `Compress-Archive -> SHA-256 -> evidence -> upload`
and exact output wiring; merely containing the string `Get-FileHash` is insufficient.

Because the overall PF4 branch intentionally remains red on upstream production defects, the
packaged M12 job is skipped after integrated verification fails. Therefore this branch currently has
**static/executable workflow-regression proof**, not a completed packaged-run proof of the new hash
step. Runtime packaged proof becomes mandatory once upstream acceptance failures are repaired enough
for M12 to reach that job.

## C0–C5 scale and long-horizon results

The most important positive result is that Product Factory scale/restart foundations are materially
stronger than the remaining trust-boundary defects suggest.

### C0 — deterministic single component: PASS

- one-component repository graph;
- deterministic work identity;
- exactly one ready request.

### C1 — dependency/review chain: PASS

- eight components;
- dependency-ready scheduling;
- worker result;
- independent review;
- next dependency released only after acceptance.

### C2 — multi-repository product: PASS for deterministic scheduling

- twelve components across four repositories;
- cross-repository dependency ordering;
- stable logical ownership attribution.

Physical-repository alias rejection remains one of the 14 blockers, so C2 does not imply PF3 gate
closure.

### C3 — platform variation: PASS for node contract

- Windows and Linux behind one execution-node contract;
- exact platform selection;
- distinct lease identities;
- unavailable macOS path fails closed instead of returning synthetic success.

This is provider-neutral execution evidence, not real external infrastructure acceptance.

### C4 — large graph and independent progress: PASS

- 100-component dependency chain;
- ten restart boundaries through that chain;
- 100-component team fan-out;
- 50 parallel non-overlapping ownership leases;
- one blocked component does not freeze 99 independent components.

Fixtures are generated rather than hard-coded to one expense-app example so the proof measures
factory behavior instead of memorized product topology.

### C5 — restart, review and supersession: PARTIAL / strong positive foundation

PASS:

- `REVIEW_REQUIRED` survives twenty restart cycles without silent promotion;
- an attempt-1 worker result is rejected after repair attempt 2 starts;
- corrected current-ProductProject rebind rejects stale durable checkpoint identity.

BLOCKED:

- forged accepted coordinator state;
- nested request project/repository identity drift;
- restored scope/permission expansion.

Thus long-horizon persistence works in meaningful scenarios, while semantic restore validation still
needs hardening.

## PF8 / PF9 reuse qualification

PF4 did not create a second experiment/runtime subsystem merely to make PF8/PF9 look implemented.
The existing durable Experiment Engine already supplies useful reusable primitives:

- immutable candidate artifact references;
- permission fingerprints;
- replay datasets;
- primary metrics plus guardrails;
- deterministic champion/challenger evaluation;
- durable SQLite restart;
- promotion and rollback;
- append-only evidence constraints.

PF9 business-shaped qualification uses CONFIG candidates for a pricing experiment, three segments,
conversion as the primary metric and compliance as a guardrail. It proves durable mid-experiment
restart, deterministic promotion, rollback, permission-widening rejection and a case where higher
conversion is **not** promoted because compliance regresses beyond policy.

This is a reusable controlled-experiment foundation, **not PF9 completion**. ProductProject→Business
Experiment binding, budgets, typed business artifacts and external-action approval/provider boundaries
remain separate work.

PF8 can reuse the same experiment machinery to compare bounded repair candidates and reuse integrated
deployment/operations pieces, but no complete product-level
`incident -> bounded repair -> independent review -> exact release -> staged health -> monitor -> rollback`
lifecycle is proven here. PF8 remains NOT PROVEN.

## PF0–PF12 current classification from this matrix

| Gate | Current PF4 classification | Current executable truth |
| --- | --- | --- |
| PF0 ProductProject | PARTIAL | durable/restart foundation; corrected stale rebind passes |
| PF1 Research→Product | PARTIAL | phantom evidence + duplicate requirement attacks now pass; full research journey still separate |
| PF2 Team/orchestration | PARTIAL/BLOCKED | 100-component scale passes; phantom specialist and coordinator semantic restore fail |
| PF3 Repository graph | PARTIAL/BLOCKED | large/multi-repo scheduling works; repo alias/decision/identity attacks fail |
| PF4 Implementation | BLOCKED | review/restart works; acceptance-command completeness fails |
| PF5 Execution/Command Center | PARTIAL | platform node contract works; packaged Product Factory composition absent |
| PF6 Deployment | PARTIAL | project-scoping/rollback/restore attacks now pass; real-provider/product acceptance separate |
| PF7 Credentials | PARTIAL/BLOCKED | raw-secret and real restart-handle tests pass; audit counter monotonicity fails |
| PF8 Operations | NOT PROVEN | reusable experiment/deployment/operations pieces, no full incident lifecycle |
| PF9 Business Factory | PARTIAL FOUNDATION | controlled durable business experiment qualifies; full business factory absent |
| PF10 IP/license | BLOCKED | notices builder strong, names-only verifier false-green remains |
| PF11 Representative packaged E2E | BLOCKED | command composition missing; package/human/NVDA journey not proven |
| PF12 Long horizon | PARTIAL/BLOCKED | scale/restarts/supersession pass; semantic forged-restore attacks fail |

No row marked PARTIAL is a Product Factory completion claim.

## External technical baseline used by the matrix

The matrix was cross-checked against primary guidance to derive useful invariants, not to claim a
standards certification.

### GitHub rulesets / protected branches

Primary references:

- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

Useful invariant: required checks and merge protection should apply to the latest intended candidate,
with strict/up-to-date semantics where that is the policy.

Live repository metadata continues to report `main.protected=false`, protection disabled and required
status-check enforcement off. Exact-head Core/M12 discipline therefore remains a project process, not
a mechanically enforced GitHub invariant. PF11 cannot claim protected-main governance until an
appropriate ruleset/branch-protection policy exists.

### OWASP Secrets Management lifecycle

Primary reference:

- https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

Useful invariants: least privilege, rotation, revocation, expiry, auditability and effective access
termination. PF4 uses these to distinguish OS material persistence from process-ephemeral handle
validity and to require monotonic/reconstructable credential audit evidence.

### SLSA provenance output identity

Primary reference:

- https://slsa.dev/spec/v1.1/requirements

Useful invariant: evidence must unambiguously identify the produced artifact by cryptographic digest.
PF4 therefore binds pre-human evidence to the **final outer ZIP**, not merely hashes of unpacked bundle
files.

### SPDX / CycloneDX license evidence

Primary references:

- https://spdx.dev/use/specifications/
- https://cyclonedx.org/docs/1.7/json/
- https://cyclonedx.org/specification/overview/

Useful invariant: package presence is not equivalent to declared/observed/concluded license evidence.
That is why a names-only THIRD_PARTY_NOTICES file remains an executable PF10 rejection case.

## Exact-head evidence policy

A candidate may receive acceptance credit only when the relevant evidence refers to the same exact
head SHA and current-main compatibility has not become stale. For a packaged candidate, that includes:

1. branch head;
2. checkout identity;
3. Core result;
4. applicable M12 result;
5. PF4 acceptance matrix;
6. release manifest source SHA;
7. exact final distributable ZIP digest;
8. human/NVDA flags remaining false unless actual human/NVDA evidence exists.

## Current cross-lane integration notes

- PF1 #125 has exact source verification green but full M12 red on the shared packaged WebView2/UIA
  proof; PF1 must not duplicate that shared UIA repair.
- PF2 program-host successor #127 has demonstrated a clean full source/recovery matrix on a historical
  exact head, while a separate M9 browser-download proof failed outside its PF2 diff; current head
  requires fresh complete evidence.
- stale PF5 #100 must not be merged; current PF5 successors must consume the repaired current PF1/PF3
  contracts.
- two simultaneously active PF5 current-main successors (#128 and #129) overlap six files directly;
  PF4 treats that as an ownership collision. They must converge to one successor before integration,
  carrying forward the unique decision/credential coverage from #128 and newer PF3 operations/fleet
  coverage from #129 rather than overwriting one another.

## Non-claims

This matrix does not claim:

- real cloud/SSH deployment;
- real remote-machine execution;
- real payment or DNS mutation;
- arbitrary external credential-provider acceptance;
- full PF8 Operations lifecycle;
- full PF9 Business Factory;
- PF10 release compliance completion;
- PF11 packaged representative Product Factory completion;
- human accessibility completion;
- NVDA verification;
- production release readiness.

Those claims require their own owned implementation and evidence gates.
