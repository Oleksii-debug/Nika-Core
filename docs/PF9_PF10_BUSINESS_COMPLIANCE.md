# PF9 Business Factory and PF10 compliance contracts

## Scope

This document records the production boundary introduced for PF9 Autonomous Business Factory and the ProductProject-level PF10 IP/license/compliance gate.

The supported lifecycle is:

`Business Goal -> Market Research -> Opportunity -> Lead/Channel -> Qualification -> Proposal -> Approval/Policy -> WorkOrder/ProductProject -> Product Factory -> QA -> Delivery -> Invoice/Payment State -> Support`.

The implementation deliberately does **not** provide autonomous external-message sending, contract signing, account creation, publishing, payment execution, CAPTCHA bypass, impersonation, or authority expansion.

## REUSE -> ADAPT -> CUSTOM (thin)

### REUSE

- Universal Research remains the research system. `BusinessObjective` consumes the existing `ResearchEvidencePackage` and `EvidenceRef` contracts; PF9 does not implement a crawler or a second research engine.
- Product Factory remains the build system. A Business `WorkOrder` hands off through the existing `ProductProjectRepository.create(...)`; PF9 does not implement a second product-build coordinator.
- Nika's existing `SQLiteStore.connection()` transaction boundary is reused for durable PF9 snapshots and communication records.
- ProductProject's existing durable idempotency ledger is reused to reconcile an uncertain WorkOrder handoff rather than creating a second side-effect ledger.
- Existing packaging notice generation/verification remains responsible for Nika runtime bundle notices. PF10 project compliance records dependency-specific notice/obligation evidence instead of replacing that packaging subsystem.
- Python stdlib HMAC/SHA-256 is used only for a process-local positive-decision integrity proof; no new crypto or policy framework is introduced.

### ADAPT

- Research evidence IDs are constrained to the exact objective research package before an opportunity can be created.
- ProductProject handoff is bound to an authorized WorkOrder and a PF9-derived durable operation identity.
- Communication policy is adapted into durable `DRAFT -> AUTHORIZED -> SENT/FAILED` evidence without adding a sender to PF9 core.
- PF10 dependency records add the project-level source/version/license/provenance, authorized review evidence and distribution-obligation decision needed before delivery.
- `ComplianceReviewAuthorityPort` is a framework-neutral dependency-inversion boundary for resolving review/legal/permission evidence. It is intended to adapt the canonical Nika approval/review authority after that authority is integrated; PF10 does not create a parallel signer.

### CUSTOM (thin)

Nika-specific custom code is limited to the Business lifecycle state machine, policy authority boundaries, restart-safe snapshot/communication persistence, append-only ordered audit evidence, WorkOrder handoff binding and PF10 release/delivery compliance semantics.

## Policy and authority boundaries

`BusinessPolicy` is fail-closed:

- Lead creation is allowed only on declared channel IDs.
- Communication may be draft-only, separately approved, or covered by an explicit standing-policy reference.
- Contract authority remains approval-required. The PF9 contract has no autonomous contract-signing mode.
- Financial authority is record-only. PF9 stores invoice/payment state supplied by an external evidence reference and has no payment executor.
- Proposal approval and WorkOrder authorization are separate evidence references.
- Delivery requires a separate authorization reference, passing QA, the exact linked ProductProject identity, and an allowed PF10 compliance decision.

These contracts do not grant a provider/account credential and do not bypass Nika's R0-R4 approval/security boundary.

## Durable communication state

`BusinessCommunicationCoordinator` is a record/authorization boundary, not an external provider adapter.

- `DRAFT_ONLY` policy can create a durable draft but can never authorize sending.
- `APPROVAL_REQUIRED` requires a separate exact approval reference before a draft becomes `AUTHORIZED`.
- `STANDING_POLICY` binds authorization to the exact standing-policy reference already present in `BusinessPolicy`; a caller cannot substitute its own approval reference.
- Objective, policy, lead and channel identities are revalidated before authorization and before provider-result recording. Policy/lead/channel drift requires a fresh draft.
- PF9 records exactly one provider success evidence reference (`SENT`) or one provider failure evidence reference (`FAILED`) after authorization.
- Terminal provider results cannot be replayed through the same record.
- SQLite persistence uses optimistic row versions and rejects stale writers/corrupt snapshots after restart.

PF9 core intentionally has no `send`, `publish`, account-login, CAPTCHA-bypass or payment method. A future provider adapter must separately obey the platform API/terms/security boundary and must reconcile uncertain external effects before recording a terminal result. This lane does not claim a live external provider proof.

## WorkOrder -> ProductProject authority and crash recovery

The ProductProject handoff treats the authorized PF9 WorkOrder as authority, not caller-supplied ProductProject identity.

Before any durable ProductProject effect:

- the supplied `ProductProjectSpec.compliance.business_work_order_ref` must equal the exact current authorized WorkOrder;
- PF9 adds the exact WorkOrder authorization reference and BusinessObjective reference to the stored ProductProject compliance metadata;
- the caller request key is retained only as lineage evidence;
- the actual ProductProject idempotency operation key is deterministically derived by PF9 from `(objective_id, work_order_id)` and is therefore not caller-controlled.

This closes the crash window between the ProductProject commit and the later PF9 aggregate save. If the ProductProject effect committed but the PF9 link did not, an exact retry uses the same durable ProductProject operation identity and can reconcile the already-created project. A retry that tries to substitute a new project/name/spec/request identity for the same WorkOrder conflicts with that durable idempotency record and fails closed instead of creating a second ProductProject.

An already-linked WorkOrder also rechecks that the durable ProductProject exists and carries the expected WorkOrder binding before returning it.

## PF10 compliance model

`DependencyAdoption` requires exact non-empty:

- project/component identity;
- package name and version;
- source reference;
- provenance reference;
- recorded license expression;
- license disposition from an authorized review/policy process.

An `APPROVED` license disposition is release-allowing and therefore requires a durable `review_ref`; a bare caller-supplied enum is not sufficient evidence. `REVIEW_REQUIRED` and `BLOCKED` remain release-blocking.

Every declared distribution obligation must have matching fulfillment evidence. Duplicate fulfillment records for the same component/obligation are ambiguous and block; fulfillment evidence for an undeclared component is orphan evidence and blocks. A component marked as requiring notices must carry notice references. Missing/unacceptable provenance, license disposition, obligations, or notice evidence blocks the compliance decision and therefore blocks PF9 delivery.

Competitor evidence is permitted only when it is recorded as permitted public evidence with a durable permission/terms-policy basis reference, or when proprietary material carries both a legal-basis reference and an explicit reuse-authorization reference. Possession, public visibility or access alone is not treated as permission to copy/reuse.

A compliance inventory cannot become release-allowing merely because every evidence tuple was omitted. Products with legitimately empty dependency/competitor inventories may supply a `scope_review_ref`, but that reference is not authority by itself.

### Review and legal authority resolution

PF10 treats opaque evidence references as identifiers, not as proof that a review occurred. Every release-allowing reference must be resolved through `ComplianceReviewAuthorityPort.verify(project_id, evidence_ref, purpose)`.

The resolver binding includes all three dimensions:

- exact ProductProject identity;
- exact evidence reference;
- exact purpose, such as `compliance-scope`, `license-disposition:<component>`, `public-source-permission:<evidence>`, `proprietary-legal-basis:<evidence>` or `proprietary-reuse-authorization:<evidence>`.

`ProductComplianceGate()` has no default trusted resolver. Without a resolver, arbitrary strings such as `caller:claims-review-happened` fail closed with an explicit untrusted-authority finding. A resolver exception or a result other than the literal Boolean `True` also fails closed.

Tests use deterministic fake implementations of this port to prove exact project/ref/purpose binding. Those fakes are test composition only and do not represent production approval authority.

Current `main` does not yet provide an integrated authenticated M10/R4 approval/review contract suitable for this PF10 adapter. Active security work owns that authority surface. DEV30 therefore does not invent a second HMAC signer or consume an unmerged sibling contract. Until a canonical trusted resolver is integrated and adapted, PF10 production positive-authority integration remains blocked even if this framework-neutral contract and its deterministic tests are green.

### Positive decision authority

`ProductComplianceDecision` is a result, not caller-owned release authority. A caller may still construct the value type for negative/reporting use, but a caller-constructed `allowed=True` object has no positive authority: its effective `allowed` value is false at the PF9 delivery boundary.

`ProductComplianceGate.evaluate(...)` is the only production path that issues a positive decision after all applicable input references resolve through the trusted review-authority port. It binds the project ID, decision state, findings and exact evidence-reference set with a process-local HMAC-SHA256 integrity proof. Copying/tampering with a valid decision, including project or evidence substitution, invalidates the positive authority.

This proof is deliberately **not** described as a durable signature, human approval, secret-store authority or hostile-code sandbox. The key exists only in the current trusted Python process. Same-process code with unrestricted module-memory access is outside this contract's containment claim. A positive decision is therefore short-lived and should be regenerated from durable compliance evidence after process restart rather than persisted as an authority token.

## Persistence and restart integrity

`BusinessFactoryRepository` stores one canonical JSON aggregate per BusinessObjective through Nika's SQLite transaction boundary. It uses a PF9-owned migration table rather than editing shared research/ProductProject migrations.

Writes use optimistic row-version checks. Non-advancing writes and stale writers fail closed. Restore revalidates:

- schema identity;
- objective/research evidence binding;
- opportunity/lead/proposal/work-order linkage;
- allowed channel policy;
- ProductProject linkage;
- QA-before-delivery ordering;
- delivery-before-payment/support ordering;
- contiguous audit sequence and row-version equality.

`BusinessCommunicationRepository` uses a separate PF9-owned migration stream in the same canonical SQLite store so communication-state evolution does not edit shared ProductProject or Universal Research migrations.

## Acceptance evidence in this lane

Focused regressions cover:

- complete Business flow into the real durable ProductProject repository;
- exact WorkOrder binding before ProductProject creation;
- uncertain ProductProject handoff exact-retry reconciliation and duplicate-identity rejection;
- SQLite restart and continuation;
- durable communication authorization/result/restart/stale-writer behavior;
- draft-only and standing-policy communication authority boundaries;
- unknown research evidence rejection;
- unapproved channel rejection;
- qualification/approval ordering;
- contract and money authority non-expansion;
- QA and exact-project PF10 delivery gate;
- caller-fabricated positive compliance decision rejection at the real PF9 delivery boundary;
- positive decision project/evidence tamper invalidation;
- opaque scope-review and approved-license review strings rejected without trusted resolver;
- exact project/ref/purpose review-authority binding;
- resolver exceptions fail closed;
- reviewed-empty compliance positive control only through an explicitly trusted test resolver;
- forged snapshot and stale-writer rejection;
- missing dependency version/provenance rejection;
- missing approved-license review evidence;
- blocked/review-required license rejection;
- missing/duplicate/orphan distribution fulfillment and missing notices;
- public competitor permission-basis and proprietary-copy authorization boundaries.

Exact-head CI remains the source-quality evidence source. This document does not assign `HUMAN_TESTED` or `NVDA_VERIFIED`, does not claim that any real external business provider has been exercised, and does not claim PF10 production authority is integrated before the canonical review-authority adapter exists.
