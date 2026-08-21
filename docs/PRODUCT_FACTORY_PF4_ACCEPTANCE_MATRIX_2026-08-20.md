# PF4 Product Factory deep acceptance matrix — 2026-08-20 / reconciled 2026-08-21

## Purpose and ownership

PF4 is the independent Product Factory integration-gatekeeper and acceptance-QA lane. It owns executable acceptance tests, QA/integration fixtures and harnesses, compatibility evidence, exact-candidate lineage, and independent audit/rejection. It must not become a second feature developer.

Drive routing truth explicitly assigns release plumbing and representative PF11 assembly to AUTO-PF5. Therefore PF4 may **test** the M12 release workflow, but it must not repair the release workflow merely to turn its own acceptance test green. Product defects are handed to the exact production owner.

The current compatibility baseline is exact live main:

`449ed6dc34e8911aa2759b2a1219fd2720d11dd8`

Current main contains merged PF3 rolling fleet maintenance exact candidate `78550c79a54a8581e3dbf864094a9a5df2ee620f` after Core #802 and M12 #570 succeeded. Repository metadata still reports `main.protected=false` and required-check enforcement off.

Acceptance policy:

- a RED adversarial test is useful evidence when integrated production behavior violates an invariant;
- production-owner failures are never marked `xfail`, skipped, or weakened to manufacture GREEN;
- PF4-owned false-positive tests are corrected when independently established;
- PF4 does not repair PF1/PF2/PF3/PF5 product behavior or manual DEV01–DEV05/M10 source;
- old exact-head results become lineage evidence after branch/main drift;
- merge-ref success does not replace exact candidate-head evidence;
- cancelled checks are not GREEN;
- automated UIA is not human NVDA evidence;
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`.

## Quantitative acceptance delta

The matrix has passed through these evidence states:

1. Initial deep matrix: **770 passed / 28 failed**.
2. Current-main refresh after integrated PF1/PF3 repairs: **1018 passed / 17 failed**.
3. PF4 self-audit identified **two PF4 false-positive assertions**. After correcting only those tests, the valid product-blocker count is **15**, not 17.

A transient exact-head run reached **1021 passed / 14 failed**, but PF4 does **not** accept that 14-blocker number as final gate truth because the same PF4 branch had crossed the ownership boundary and patched `.github/workflows/m12-prehuman-release-gate.yml` to make the outer-ZIP digest regression pass. Drive routing assigns release plumbing / PF11 assembly to PF5. PF4 has therefore restored that workflow file byte-for-byte to exact current-main content while retaining the strengthened acceptance regression. The outer-ZIP digest requirement is again an upstream PF5/release-owner blocker.

Correct accounting from the original 28 failures is therefore:

- **11 former product defects independently pass the same adversarial assertions** after production integrations;
- **2 PF4 assertions were false positives** and were corrected without product mutation;
- **15 valid product/product-journey blockers remain** pending fresh exact-head Core + M12 on the ownership-corrected PF4 head.

No acceptance credit is created by deleting a valid product failure or by having PF4 implement a production-owner repair.

## Eleven historical product failures independently proven repaired

Current main now passes the same attacks for:

1. PF6 current-release state scoped by ProductProject + environment;
2. PF6 rollback isolation across ProductProjects;
3. PF7 token-shaped durable scalar rejection;
4. PF7 raw-token-as-credential-reference rejection;
5. PF1 phantom evidence-package rejection;
6. PF1 duplicate requirement identity rejection;
7. execution restore rejection of two active leases on one node;
8. execution restore rejection of invalid lease time ordering;
9. PF6 empty environment/provider rejection;
10. PF6 exact previous-release rollback restoration;
11. PF6 coherent restored current-release snapshot requirement.

These are integration credits, not complete PF-gate closure.

## PF4 self-audit corrections

### Correction A — ProductProject freshness at the required rebind boundary

The original attack reused one already-constructed `ProductProjectCoordinatorBinding` after an out-of-band repository mutation and required that immutable binding object to discover the mutation by itself. That was not the public recovery contract.

The corrected attack checkpoints ProductProject version N, durably mutates to N+1, re-reads the current ProductProject at recovery, constructs the required new binding, and then requires the old checkpoint restore to raise `StaleProductProjectBindingError`. Current main passes that real boundary.

### Correction B — credential restart must model a new process/store instance

The original PF7 fixture reused the exact same in-memory fake protected-store object across a supposed restart. The corrected test constructs a new `WindowsCredentialStore` instance over the same OS-like backend and a new broker. The old broker lease/process-local handle is unusable after restart while legitimate protected material remains available for explicit re-issuance. Current main passes this real boundary.

## Current valid remaining product failures — 15

### PF2 coordinator semantic restore / PF12 — 4

Current coordinator restore accepts structurally valid but semantically forged state:

1. `ACCEPTED` work without worker result or independent accepted review;
2. nested request repository identity drift;
3. nested request ProductProject identity drift;
4. restored allowed-path or permission expansion.

Owner: PF2 coordinator/recovery.

### PF2 implementation-evidence completeness / PF4 — 2

1. one easy passing command can stand in for multiple declared `acceptance_commands`;
2. an unrelated passing command can substitute for the declared acceptance matrix.

Required invariant: every declared acceptance command must have exact corresponding successful evidence before independent review can accept the implementation.

Owner: PF2 program-host / coding-worker adapter / coordinator reconciliation.

### PF2/PF3 team and repository integrity — 5

1. the same physical provider/locator repository can hide behind multiple logical repository IDs;
2. an integration decision can omit the actual conflicting active lease;
3. a dynamic specialist can own a phantom component absent from the project component set;
4. an empty opaque repository credential identity such as `credref:` is accepted;
5. an empty ProductComponent identity is accepted.

Owner: PF2/PF3 repository/team orchestration.

### PF7 credential audit monotonicity — 1

`CredentialBroker.restore()` accepts an audit-event counter rolled back behind already persisted event identities, allowing future durable identity reuse.

Owner: PF7 credential broker.

### PF10 license/notices verification — 1

A deliberately names-only `THIRD_PARTY_NOTICES.txt` still passes `verify_third_party_notices()` without meaningful license evidence.

Owner: PF5/release-compliance packaging surface.

### PF11 packaged Product Factory composition — 1

`scripts/nika_windows.py` still does not compose the durable ProductProject/Product Factory `route_command` path required for the representative packaged factory journey.

Owner: PF5 packaged Product Factory composition. Shared UI/UIA remains separately owned by the Interaction lane.

### PF11 final distributable identity — 1

Current-main M12 creates the outer `NikaCore-<version>-windows-x64.zip` and then writes pre-human evidence without recording SHA-256 of that exact final uploaded ZIP. Internal release-manifest hashes do not identify the outer distributable artifact.

PF4 owns the regression, not the repair. The acceptance test requires semantic order `Compress-Archive -> hash exact ZIP -> record evidence -> upload` and exact digest wiring. PF4 restored the M12 workflow to current-main content after detecting its own ownership violation.

Owner: PF5 exact-SHA release integration / release plumbing.

## C0–C5 scale and long-horizon evidence

### C0 — PASS foundation

Deterministic single component, stable work identity, exactly one ready request.

### C1 — PASS foundation

Eight-component dependency/review chain; next dependency releases only after worker evidence and independent acceptance.

### C2 — scheduling foundation PASS / gate BLOCKED

Twelve components across four logical repositories schedule deterministically. Physical-repository alias rejection remains a blocker, so C2 does not close PF3.

### C3 — provider-neutral node contract PASS / real-provider acceptance absent

Windows and Linux use one execution-node contract with exact platform selection and distinct lease identities; unavailable macOS fails closed. This is not proof of a real remote provider or host.

### C4 — scale foundation PASS

- 100-component dependency chain;
- ten restart boundaries;
- 100-component team fan-out;
- 50 parallel non-overlapping ownership leases;
- 99 independent components progress around one blocker;
- integrated PF3 adds 60-service / 180-replica fleet and rolling-maintenance evidence.

Real-provider selected verticals remain separate acceptance work.

### C5 — PARTIAL / BLOCKED

PASS:

- `REVIEW_REQUIRED` persists through twenty restarts;
- superseded attempt-1 result is rejected after repair attempt 2 starts;
- corrected current-ProductProject rebind rejects stale checkpoint identity.

BLOCKED:

- forged accepted coordinator state;
- nested request project/repository identity drift;
- restored path/permission expansion.

## PF8 / PF9 reuse qualification

PF4 does not create duplicate experiment/runtime subsystems. Existing durable Experiment Engine primitives provide reusable candidate refs, permission fingerprints, replay datasets, primary metrics/guardrails, deterministic champion/challenger evaluation, durable SQLite restart, promotion/rollback and append-only evidence constraints.

PF9 has useful controlled durable business-experiment foundation evidence, including a higher-conversion candidate that is not promoted when compliance violates policy. That is not full Business Factory completion.

PF8 can reuse experiment, deployment and operations foundations, but no complete product-level `incident -> bounded repair -> independent review -> exact release -> staged health -> monitor -> rollback` journey is proven.

## PF0–PF12 current classification

| Gate | PF4 classification | Current executable truth |
| --- | --- | --- |
| PF0 ProductProject | PARTIAL | durable/restart foundation; corrected stale rebind passes |
| PF1 Research→Product | PARTIAL | phantom evidence + duplicate requirement attacks pass; full journey separate |
| PF2 Team/orchestration | PARTIAL/BLOCKED | scale passes; phantom specialist + semantic restore fail |
| PF3 Repository graph | PARTIAL/BLOCKED | multi-repo scheduling works; alias/decision/identity attacks fail |
| PF4 Implementation | BLOCKED | review/restart works; acceptance-command completeness fails |
| PF5 Execution/Command Center | PARTIAL | provider-neutral node contract works; packaged composition/release truth blocked |
| PF6 Deployment | PARTIAL | project scoping/rollback/restore attacks pass; real-provider journey separate |
| PF7 Credentials | PARTIAL/BLOCKED | raw-secret and real restart-handle attacks pass; audit monotonicity fails |
| PF8 Operations | NOT PROVEN | reusable foundations; no full maintenance/repair/release lifecycle |
| PF9 Business Factory | PARTIAL FOUNDATION | controlled durable experiment, not full factory |
| PF10 IP/license | BLOCKED | names-only notices verifier false-green remains |
| PF11 Representative packaged E2E | BLOCKED | command composition + final ZIP identity + governance/human/NVDA remain |
| PF12 Long horizon | PARTIAL/BLOCKED | scale/restart/supersession pass; forged semantic restore fails |

No PARTIAL row is a Product Factory completion claim.

## Live cross-lane gatekeeper notes

### PF1 #125

Current exact head `7ab89f8d5ac625e23e8764996e5f9d3caae2a713`, base exact current main, draft. Ubuntu Core #831 and Ubuntu M12 #599 are GREEN; Windows portions were still running at the latest PF4 read. No final GREEN or merge credit until complete exact-head Core + M12 and draft/review boundary are satisfied.

### PF2 #127

Current exact head `31fdda3bc7a8bece22100c3150b5eba1002b6ae3`, exact current-main base, draft. It supersedes stale #118 and contains exactly three PF2-owned program-host/test files. Fresh exact-head Core + M12 are mandatory. Earlier successor heads proved the old Ruff `I001` family repaired and exercised crash/cancellation semantics, but superseded SHA evidence does not transfer.

### PF5 #128 / #129 ownership collision

Two live PF5 current-main successors exist simultaneously:

- #128 head `f6ab4494d4a0d5e7b574f64b973df88051e59c9b`;
- #129 current live head `c0f3f3f1a6b565e94781aac59de04a4643d8954d`.

They overlap exactly these six files:

- `src/nika_core/product_command/__init__.py`;
- `src/nika_core/product_command/command_center.py`;
- `src/nika_core/product_command/contracts.py`;
- `src/nika_core/product_command/credential_adapter.py`;
- `src/nika_core/product_command/product_project_adapter.py`;
- `tests/test_product_command_product_project_adapter.py`.

PF4 therefore classifies them as an active same-lane ownership collision. They must converge to one authoritative PF5 successor before integration; neither should overwrite the other merely because its CI happens to finish first. Unique decision/credential coverage and newer PF3 operations/fleet coverage must be intentionally reconciled by PF5.

## External technical baseline

PF4 uses primary guidance to derive acceptance invariants, not to claim standards certification:

- GitHub protected branches/rulesets: required latest-head checks need repository enforcement for PF11 governance;
- OWASP Secrets Management lifecycle: least privilege, rotation/revocation/expiry/auditability;
- SLSA provenance: output evidence must identify the produced artifact by cryptographic digest;
- SPDX/CycloneDX: package-name presence is not equivalent to license evidence.

The final-ZIP digest invariant is therefore retained as a PF4 rejection test and handed to PF5/release plumbing rather than implemented by PF4.

## Exact-head evidence policy

A candidate receives acceptance credit only when evidence refers to the same exact head SHA and current-main compatibility is still valid. Relevant boundaries include branch head, checkout identity, Core, applicable M12, PF4 matrix, release-manifest source SHA, exact final distributable ZIP digest, and human/NVDA flags remaining false unless actual human evidence exists.

## Non-claims

This matrix does not claim real cloud/SSH deployment, real remote-machine execution, real payment/DNS mutation, arbitrary external credential-provider acceptance, full PF8, full PF9, PF10 completion, PF11 packaged representative Product Factory completion, human accessibility completion, NVDA verification, or production release readiness.
