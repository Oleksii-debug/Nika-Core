# PF4 Product Factory deep acceptance matrix — 2026-08-20

## Evidence identity

- PF4 branch: `auto-pf4/product-factory-acceptance-matrix`
- exact starting main: `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`
- starting main event: integrated PF3/PF7 opaque credential broker foundation
- this batch owns QA tests and acceptance evidence only
- this batch does not modify PF1/PF2/PF3/PF5 production source, manual DEV01–DEV05 source,
  M10 source, shared DesktopBackend, WebView2, UIA, ModelGateway internals, or release code
- `HUMAN_TESTED=false`
- `NVDA_VERIFIED=false`
- `PF11=false` until the packaged natural-language Product Factory journey is executable

The purpose of this matrix is not to make CI green by weakening expectations. A red test is useful
acceptance evidence when it proves that integrated production behavior violates a binding Product
Factory invariant. Red tests must be repaired by the owning production lane, not marked `xfail` or
rewritten to accept the defect.

## Current external technical baseline

The matrix was cross-checked against current primary guidance instead of relying only on old project
notes.

### GitHub repository protection

Primary sources:

- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

Relevant current rules:

- require a pull request before merging;
- require status checks before merging;
- strict status checks require the topic branch to be up to date with the target branch;
- required checks must apply to the latest candidate SHA;
- rulesets can block force pushes and restrict bypass actors.

Live repository evidence at this batch start reports `main.protected=false` and required status check
enforcement `off`. Therefore exact-head Core/M12 discipline currently exists as a project process,
not as a GitHub-enforced invariant. PF11 cannot treat process-only protection as equivalent to an
enforced protected-main gate.

### Secret lifecycle

Primary source:

- https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

Acceptance implications:

- creation uses least privilege;
- rotation, revocation and expiration are distinct lifecycle operations;
- revocation must actually restrict access;
- dynamic credentials should stop working when the consumer no longer lives;
- secrets must not be logged;
- audit evidence should make use/rotation/revocation reconstructable.

PF7 therefore tests not only that broker in-memory leases disappear after restart, but that protected
handles from pre-restart leases do not remain usable merely because the broker forgot them.

### Build and artifact provenance

Primary source:

- https://slsa.dev/spec/v1.1/requirements

Relevant acceptance implication: provenance must unambiguously identify the output package by a
cryptographic digest and describe how it was produced. Nika's internal release manifest hashes files
inside `dist/NikaCore`, but the M12 distributable ZIP is created later. PF11 therefore requires a
digest for the actual outer ZIP delivered to the user, not only hashes of its unpacked contents.

### License and BOM evidence

Primary sources:

- https://spdx.dev/use/specifications/
- https://cyclonedx.org/docs/1.7/json/
- https://cyclonedx.org/specification/overview/

Current SPDX specification is 3.0. CycloneDX 1.7 distinguishes declared, concluded and observed
license evidence. PF10 consequently distinguishes "a package name appears in notices" from evidence
that a license was actually declared/observed/concluded. The present acceptance suite attacks a
names-only `THIRD_PARTY_NOTICES.txt` because mere name presence is not license provenance.

## PF0–PF12 acceptance map

| Gate | Required proof in this batch | Executable evidence | Starting classification |
| --- | --- | --- | --- |
| PF0 ProductProject | durable current identity; stale state cannot resume | stale durable mutation against existing binding | BLOCKED |
| PF1 Research→Product | provenance references resolve; requirement IDs unambiguous | phantom evidence and duplicate requirement attacks | BLOCKED |
| PF2 Team Composer | bounded real component ownership; permission attenuation | phantom specialist component; 100-component team | PARTIAL |
| PF3 Repository graph | physical repo identity cannot be aliased; conflict decisions name owners | repo alias and malformed decision attacks | BLOCKED |
| PF4 Implementation | every required acceptance command runs; no self-promotion | incomplete/unrelated test evidence; review-required restart | BLOCKED |
| PF5 Build nodes | two platform identities; restored lease state remains valid | Windows/Linux routing; duplicate-node-lease attack | PARTIAL |
| PF6 Deployment | project-scoped env state; exact rollback; restore revalidation | cross-project env, wrong rollback SHA, corrupt restore | BLOCKED |
| PF7 Credentials | raw-secret rejection; scoped leases; restart invalidation; audit monotonicity | scalar secret, raw cred ref, handle restart, counter rollback | BLOCKED |
| PF8 Operations | durable incident→repair→release→monitor/rollback | no complete integrated operation cycle yet | NOT PROVEN |
| PF9 Business Factory | sandboxed business artifact path and approval boundary | no integrated qualifying surface yet | NOT PROVEN |
| PF10 IP/license | package license evidence and fail-closed policy | names-only notices attack | BLOCKED |
| PF11 E2E Factory | packaged command→ProductProject→factory→artifact with exact digest | composition-root and ZIP digest gates | BLOCKED |
| PF12 Long horizon | repeated restart, repair, superseded attempt/spec, no stale promotion | 20/10 restart cycles, forged snapshot attacks | BLOCKED |

No gate is promoted merely because a lower-level unit test is green.

## Complexity ladder C0–C5

### C0 — one component

Executable proof:

- one-component deterministic ProductRepositoryGraph;
- deterministic coordinator request identity;
- exactly one ready request.

This proves only the smallest scheduling contract. It does not prove PF11.

### C1 — modest dependency chain

Executable proof:

- eight components;
- dependency-ready scheduling;
- worker result;
- independent review;
- next dependency released only after acceptance.

### C2 — multi-repository product

Executable proof:

- twelve components;
- four repository identities;
- cross-repository dependency ordering;
- stable ownership attribution.

Adversarial extension rejects a physical repository represented under multiple logical IDs, because
that would let overlapping paths escape per-repository ownership checks.

### C3 — platform variation

Executable proof:

- Windows and Linux execution nodes behind one contract;
- exact platform selection;
- distinct node lease identities;
- unavailable macOS path fails closed rather than returning synthetic success.

A future C3 browser/payment scenario still needs semantic DOM mutation recovery, explicit simulated
checkout and approval boundaries. It is not inferred from node routing.

### C4 — large product graph

Executable proof:

- 100-component dependency chain;
- ten restart boundaries during the chain;
- 100-component large-team fan-out;
- 50 parallel non-overlapping ownership leases;
- one blocked component does not freeze 99 independent components.

The matrix deliberately uses generated synthetic components rather than a hard-coded expense-app
fixture so the proof tests factory behavior rather than one product template.

### C5 — long horizon and supersession

Executable proof:

- `REVIEW_REQUIRED` survives twenty restart cycles without promotion;
- superseded attempt result is rejected after repair attempt 2 begins;
- stale durable ProductProject mutation invalidates old orchestration;
- forged accepted checkpoint is rejected;
- request project/repository/path/permission drift is rejected on restore.

The last four are presently expected to expose integrated gaps. They are not `xfail` candidates.

## Adversarial families and ownership handoff

### A. Durable ProductProject freshness

Attack:

1. create durable ProductProject;
2. construct binding;
3. checkpoint coordinator;
4. mutate durable ProductProject through PF1 repository;
5. reuse the *same old binding*;
6. require stale classification.

Why this matters: a test that constructs a fresh binding after mutation proves only the happy caller,
not that the recovery boundary itself prevents cached durable truth from being reused.

Owner: PF2 binding/checkpoint host, consuming PF1 durable state.

### B. Coordinator semantic restore

Attacks:

- `ACCEPTED` without result/review;
- request repository ID drift;
- request project ID drift;
- path expansion;
- permission expansion.

Checksum integrity is not semantic integrity. A serialized object can be perfectly checksummed and
still violate the repository graph or independent-review contract.

Owner: PF2 coordinator/recovery.

### C. Acceptance evidence completeness

Attacks:

- component declares two acceptance commands but worker reports only one;
- worker reports an unrelated easy passing command.

A non-empty tuple of exit-code-zero tests is not equivalent to the declared acceptance matrix.

Owner: PF2 coding-worker adapter/coordinator reconciliation.

### D. Repository ownership identity

Attacks:

- same provider/locator represented by two repository IDs;
- integration decision repeats candidate lease and omits the actual active conflicting owner;
- empty component identity;
- empty opaque credential identity `credref:`.

Owner: PF2/PF3 repository graph.

### E. Execution-node restart integrity

Attacks:

- two active WorkLease records restored onto one node;
- lease expires before issuance.

Owner: PF3 execution node registry.

### F. Deployment identity and rollback

Attacks:

- product A and product B both use environment ID `shared-staging`;
- product B must not inherit product A's previous release SHA;
- rollback evidence claiming a different restored SHA than the recorded previous release must fail;
- restored `current_releases` must be backed by coherent snapshot records;
- empty environment/provider identity must fail.

Owner: PF3 deployment fabric; PF5 must consume the repaired project-scoped contract rather than
papering over it in presentation.

### G. Credential restart and audit

Attacks:

- pre-restart protected handle must be invalidated when active broker leases are deliberately not
  restored;
- audit event counter cannot be restored behind already-persisted event IDs;
- raw token-shaped value cannot hide under an innocent ProductProject key;
- raw token cannot be passed as a `credential_ref`.

Owner: PF3 credential broker for lease/audit behavior; PF1 ProductProject for durable raw-secret
rejection.

### H. Research provenance

Attacks:

- ProductOption names an evidence package that was never recorded;
- duplicate ProductRequirement identity.

Owner: PF1.

### I. PF10 license evidence

Attack:

- construct notices containing `Python runtime` and every runtime distribution name, but no license
  declaration/text;
- verifier must reject it.

Current package-name presence alone is insufficient proof of observed/concluded license evidence.

Owner: release/compliance packaging surface.

### J. PF11 packaged artifact identity

Attacks:

- packaged Windows composition root must import/use ProductProject routing rather than sending
  `task.create` directly to the ordinary task backend;
- M12 must calculate and record SHA-256 of the final distributable ZIP;
- automated pre-human evidence must remain `human_tested=false`, `nvda_verified=false` and
  `production_release_ready=false`.

Owner: PF5 composition and release/QA integration respectively.

## Exact-head evidence policy

A candidate receives acceptance credit only if all of the following refer to the same head SHA:

1. branch head;
2. checkout identity step;
3. Core CI result;
4. applicable M12 result;
5. PF4 acceptance tests;
6. artifact/release manifest source SHA;
7. final distributable artifact digest where a package is claimed.

Older green runs are lineage evidence only after the branch changes. A merge-ref result is not a
substitute for the exact candidate head. Cancelled checks are not green checks.

## Current governance blocker

At the exact starting main GitHub reports:

- `protected=false`;
- protection `enabled=false`;
- required status check enforcement `off`.

This does not mean existing merges were technically wrong when their exact checks were manually
verified. It means PF11 cannot yet claim that main is *mechanically protected* from direct mutation or
from merge without the required checks. The governance owner should add an active ruleset/branch
protection policy requiring PRs and the canonical exact-head checks, preferably strict/up-to-date,
without granting broad bypass.

PF4 records this as acceptance evidence; PF4 does not silently modify repository governance in a
source-test PR.

## Release truth gap relative to current standards

Current release strengths:

- exact 40-character source SHA is required by `scripts/m11_release.py`;
- internal bundle files receive SHA-256 entries;
- symlink escape is rejected;
- notices are generated before manifest;
- M12 checks exact checkout and keeps human/NVDA/release-ready flags false.

Current gaps exposed by this matrix:

- final outer ZIP digest is not recorded;
- notice verifier can accept names without proving license content;
- there is no policy proof here that an unacceptable license blocks release;
- there is no SLSA-style provenance attestation binding final ZIP digest to builder/run identity;
- PF11 Product Factory command composition is not yet present in the packaged Windows entrypoint.

The target is not to adopt standards branding for its own sake. The target is to preserve the useful
invariants: exact artifact identity, traceable build inputs, verifiable license evidence, and
fail-closed release policy.

## Non-claims

This PF4 batch does not claim:

- real cloud deployment;
- real SSH or remote-machine execution;
- real credential provider integration;
- real payment;
- real domain/DNS mutation;
- PF8 operations completion;
- PF9 business-factory completion;
- human accessibility completion;
- NVDA verification;
- production release readiness.

Those claims require their own owned implementation and evidence gates.
