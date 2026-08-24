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
- Python stdlib SHA-256 is reused for deterministic PF9 authorization fingerprints, exact ProductProject-spec identity and ProductProject handoff identity. Stdlib HMAC/SHA-256 is used only for PF10 process-local positive-decision integrity.

### ADAPT

- Research evidence IDs are constrained to the exact objective research package before an opportunity can be created.
- ProductProject handoff is bound to an authorized WorkOrder, the exact normalized initial `ProductProjectSpec`, and a PF9-derived durable operation identity.
- `BusinessAuthorizationAuthorityPort` is a framework-neutral trusted-host boundary for PF9 proposal, WorkOrder, delivery and communication authorization. It is designed to adapt the canonical M10/R4 authority after that authority is integrated; PF9 does not create a second signer.
- Communication policy is adapted into durable `DRAFT -> AUTHORIZED -> SENT/FAILED` evidence without adding a sender to PF9 core.
- PF10 dependency records add project-level source/version/license/provenance, authorized review evidence and distribution-obligation decisions needed before delivery.
- `ComplianceReviewAuthorityPort` is a separate framework-neutral boundary for resolving PF10 review/legal/permission evidence against canonical authority.

### CUSTOM (thin)

Nika-specific custom code is limited to the Business lifecycle state machine, exact business-authorization intent semantics, restart-safe snapshot/communication persistence, append-only ordered audit evidence, WorkOrder handoff binding and PF10 release/delivery compliance semantics.

## PF9 trusted authorization boundary

Opaque evidence references are identifiers, not authority. The following operations cannot turn a caller-provided non-empty string into durable authorized state:

- proposal approval;
- WorkOrder authorization;
- delivery authorization;
- explicit communication authorization;
- standing communication policy use.

Every positive PF9 authorization is represented by `BusinessAuthorizationIntent` with exact:

- BusinessObjective identity;
- purpose;
- subject identity;
- named scope bindings;
- authorization-use mode (`ONE_TIME` or `STANDING_POLICY`).

The intent has a deterministic versioned SHA-256 fingerprint. `BusinessAuthorizationAuthorityPort.authorize(intent=..., evidence_ref=...)` is the only positive authority boundary. `None`, resolver exceptions and any result other than literal `True` fail closed.

For `ONE_TIME` evidence, the trusted implementation contract requires freshness plus protection against reuse for a different intent. It may allow idempotent replay of the exact same evidence/fingerprint pair so `authority accepted -> process loss before PF9 save -> retry` can reconcile instead of permanently consuming approval without durable business state. `STANDING_POLICY` is separate: a trusted host may reuse an active policy only while that policy covers the exact requested intent; the Business worker cannot mint or widen the policy.

Current `main` does not yet provide an integrated authenticated M10/R4 authority that can implement this port. PR #61 and stacked PR #62 remain separate security-owner work and are not consumed while unmerged. Deterministic test authorities prove the port and state-machine contract only. They are not production approval evidence.

Therefore source tests may be GREEN while **PF9 production positive-authority integration remains partial/blocked** until a canonical trusted M10/R4 adapter is integrated and independently proven.

## Policy and authority boundaries

`BusinessPolicy` remains fail-closed:

- Lead creation is allowed only on declared channel IDs.
- Communication may be draft-only, separately approved, or covered by a declared standing-policy reference, but the reference itself must still resolve through the trusted host authority before `AUTHORIZED` state can be created.
- Contract authority remains approval-required. PF9 has no autonomous contract-signing mode.
- Financial authority is record-only. PF9 stores invoice/payment state supplied by an external evidence reference and has no payment executor.
- Proposal approval and WorkOrder authorization are separate exact intents and evidence references.
- Delivery requires passing QA, the exact linked ProductProject identity, an allowed PF10 compliance decision, and a separately trusted delivery authorization bound to project/artifact/QA/compliance evidence.

These contracts do not grant a provider/account credential and do not bypass Nika's R0-R4 approval/security boundary.

## Durable authorization binding

PF9 persists the exact authorization fingerprint alongside the evidence reference for approved proposals, WorkOrders and deliveries. Restore recomputes the fingerprint from persisted authority-bearing fields and fails closed if durable state was altered after authorization.

The bindings include:

- proposal: exact lead + proposal scope summary;
- WorkOrder: exact proposal + proposal approval fingerprint + WorkOrder scope +, for Product Factory handoff authority, SHA-256 of the exact normalized initial `ProductProjectSpec`;
- delivery: exact ProductProject + artifact + passing QA evidence + exact PF10 compliance-evidence references.

The ProductProject-spec fingerprint uses the same initial-spec normalization that the canonical `ProductProjectRepository.create(...)` applies: `supersedes_spec_version=None` and `revision_reason="initial specification"`, followed by canonical sorted JSON and SHA-256. This avoids authorizing one representation and executing another merely because repository-owned initial revision metadata is normalized at persistence time.

The WorkOrder authorization fingerprint and exact ProductProject-spec fingerprint are propagated into ProductProject compliance/provenance metadata. A changed WorkOrder scope or changed ProductProject spec therefore cannot silently retain the old approval identity when handed to Product Factory.

A historical/underbound WorkOrder that lacks `product_spec_fingerprint` remains loadable for compatibility and audit visibility, but it is **not ProductProject effect authority**. Handoff from such a WorkOrder fails before `ProductProjectRepository.create(...)`; a fresh exact authority decision is required rather than silently upgrading legacy evidence.

## Durable communication state

`BusinessCommunicationCoordinator` is a record/authorization boundary, not an external provider adapter.

- `DRAFT_ONLY` can create a durable draft but can never authorize sending.
- `APPROVAL_REQUIRED` requires a one-time evidence reference that the trusted host authority validates against the exact message intent.
- `STANDING_POLICY` requires the trusted authority to confirm that the active standing policy covers the exact message intent; merely putting a string into `BusinessPolicy.standing_policy_ref` does not authorize anything.
- The authorization intent binds objective, message, lead, counterparty, thread, channel, payload, policy and authorization-use mode.
- The communication record persists counterparty identity, authorization mode and exact authorization fingerprint; tampered payload/counterparty/channel/policy state fails validation.
- Objective, policy, lead, counterparty and channel identities are revalidated before authorization and provider-result recording. Drift requires a fresh draft.
- PF9 records exactly one provider success evidence reference (`SENT`) or one provider failure evidence reference (`FAILED`) after authorization.
- Terminal provider results cannot be replayed through the same record.
- SQLite persistence uses optimistic row versions and rejects stale writers/corrupt snapshots after restart.

PF9 core intentionally has no `send`, `publish`, account-login, CAPTCHA-bypass or payment method. A future provider adapter must separately obey the platform API/terms/security boundary, use the canonical approval/tool execution path at the actual side-effect boundary, and reconcile uncertain external effects before recording a terminal result. This lane does not claim a live external provider proof.

## WorkOrder -> ProductProject authority and crash recovery

The ProductProject handoff treats the authorized PF9 WorkOrder as authority, not caller-supplied ProductProject identity or a caller assertion that a WorkOrder ID is sufficient.

Before any durable ProductProject effect:

- `ProductProjectSpec.compliance.business_work_order_ref` must equal the exact current authorized WorkOrder;
- the current spec's normalized SHA-256 fingerprint must equal the fingerprint that participated in the trusted WorkOrder authorization intent;
- a WorkOrder without that exact spec authority fails closed before repository mutation;
- PF9 adds the WorkOrder authorization reference, exact WorkOrder authorization fingerprint, exact ProductProject-spec fingerprint and BusinessObjective reference to stored ProductProject compliance metadata;
- the caller request key is retained only as lineage evidence;
- the actual ProductProject idempotency operation key is deterministically derived by PF9 from `(objective_id, work_order_id)` and is therefore not caller-controlled.

This closes both the authority-substitution boundary and the crash window between ProductProject commit and later PF9 aggregate save. A caller cannot keep the authorized WorkOrder ID while replacing the ProductProject goal/outcome/requirements/risk/compliance or another spec field: the spec fingerprint mismatch is rejected before the ProductProject effect.

If the exact ProductProject effect committed but the PF9 link did not, an exact retry uses the same durable ProductProject operation identity and reconciles the already-created project. A retry that substitutes a new project/name/spec/request identity for the same WorkOrder conflicts with exact authority and/or the canonical ProductProject idempotency record and fails closed instead of creating a second ProductProject.

An already-linked WorkOrder rechecks that the durable ProductProject exists and carries the expected WorkOrder ID, WorkOrder authorization fingerprint and ProductProject-spec fingerprint before returning it.

## PF10 compliance model

`DependencyAdoption` requires exact non-empty project/component identity, package name/version, source reference, provenance reference, recorded license expression and license disposition.

An `APPROVED` license disposition is release-allowing and therefore requires a durable `review_ref`; a bare caller-supplied enum is not sufficient evidence. `REVIEW_REQUIRED` and `BLOCKED` remain release-blocking.

Every declared distribution obligation must have matching fulfillment evidence. Duplicate fulfillment records for the same component/obligation are ambiguous and block; fulfillment evidence for an undeclared component is orphan evidence and blocks. A component marked as requiring notices must carry notice references. Missing/unacceptable provenance, license disposition, obligations, or notice evidence blocks the compliance decision and therefore blocks PF9 delivery.

Competitor evidence is permitted only when recorded as permitted public evidence with a durable permission/terms-policy basis reference, or when proprietary material carries both a legal-basis reference and an explicit reuse-authorization reference. Possession, public visibility or access alone is not permission to copy/reuse.

A compliance inventory cannot become release-allowing merely because every evidence tuple was omitted. Products with legitimately empty dependency/competitor inventories may supply a `scope_review_ref`, but that reference is not authority by itself.

### PF10 review/legal authority resolution

PF10 treats opaque evidence references as identifiers, not proof that a review occurred. Every release-allowing reference must resolve through `ComplianceReviewAuthorityPort.verify(project_id, evidence_ref, purpose)` with exact ProductProject, evidence reference and purpose.

`ProductComplianceGate()` has no default trusted resolver. Arbitrary strings such as `caller:claims-review-happened` fail closed. Resolver exceptions or any result other than literal `True` also fail closed.

Tests use deterministic fake implementations only to prove exact project/ref/purpose binding. Current `main` has no integrated authenticated review/R4 authority suitable for this adapter, so PF10 production positive-authority integration also remains partial/blocked pending that dependency.

### Positive compliance decision authority

`ProductComplianceDecision` is a result, not caller-owned release authority. A caller-constructed `allowed=True` object has no positive authority at the PF9 delivery boundary.

`ProductComplianceGate.evaluate(...)` is the only production path that issues a positive decision after all applicable input references resolve through the trusted review-authority port. It binds project ID, state, findings and exact evidence-reference/input set with a process-local HMAC-SHA256 integrity proof. Copying/tampering with a valid decision, including project or evidence substitution, invalidates positive authority.

The PF10 release layer additionally binds exact release identity, project source SHA-256, delivery artifact SHA-256, verified notice-bundle SHA-256 and current compliance snapshot. Its release grant is not treated as a substitute for the still-missing canonical trusted review authority, and PF9 does not silently manufacture that authority.

These process-local proofs are deliberately **not** durable human signatures, secret-store authority or hostile-code sandboxes. They prevent ordinary caller fabrication/tamper of downstream PF10 results inside the trusted process; canonical review/approval authority remains a separate dependency.

## Persistence and restart integrity

`BusinessFactoryRepository` stores one canonical JSON aggregate per BusinessObjective through Nika's SQLite transaction boundary. It uses a PF9-owned migration table rather than editing shared research/ProductProject migrations.

Writes use optimistic row-version checks. First-writer creation uses an atomic insert/no-overwrite boundary; competing writers receive a typed stale-state result instead of relying on a raw SQLite uniqueness failure. Existing-state writes use optimistic compare-and-swap row versions. Non-advancing writes, stale writers and corrupt snapshots fail closed.

Restore revalidates schema, research binding, lifecycle linkage, channel policy, persisted authorization fingerprints, exact WorkOrder ProductProject-spec authority where a ProductProject is linked, QA-before-delivery ordering, delivery-before-payment/support ordering and contiguous audit sequence/row-version equality.

`BusinessCommunicationRepository` uses a separate PF9-owned migration stream in the same canonical SQLite store so communication-state evolution does not edit shared ProductProject or Universal Research migrations. Its first-writer and update paths follow the same typed optimistic-concurrency/fail-closed principle.

## Acceptance evidence in this lane

Focused regressions cover:

- complete Business flow into the real durable ProductProject repository;
- raw proposal/WorkOrder/delivery approval refs rejected without trusted authority;
- exact one-time intent binding, idempotent exact replay and cross-intent reuse rejection in test authority semantics;
- standing communication policy trust/revocation and exact message-scope binding;
- persisted proposal/WorkOrder/delivery/message authorization fingerprint tamper rejection;
- exact WorkOrder ID and exact normalized ProductProject-spec binding before ProductProject creation;
- same-WorkOrder/different-spec substitution rejected before any ProductProject effect;
- legacy/underbound WorkOrder prevented from creating a ProductProject effect;
- uncertain ProductProject handoff exact-retry reconciliation and duplicate-identity rejection;
- durable ProductProject WorkOrder/spec lineage revalidation after restart;
- atomic first-writer and optimistic update concurrency with typed stale outcomes;
- SQLite restart and continuation;
- durable communication authorization/result/restart/stale-writer behavior;
- unknown research evidence, unapproved channel and lifecycle ordering rejection;
- contract and money authority non-expansion;
- QA and exact-project PF10 delivery gate;
- caller-fabricated positive compliance decision rejection;
- positive compliance decision project/evidence/input tamper invalidation;
- opaque scope-review and approved-license review strings rejected without trusted resolver;
- exact project/ref/purpose review-authority binding and resolver exception fail-closed behavior;
- dependency identity/provenance/license, notices and distribution-obligation enforcement;
- public competitor permission-basis and proprietary-copy authorization boundaries.

Exact-head CI remains source-quality evidence. This document does not assign `HUMAN_TESTED` or `NVDA_VERIFIED`, does not claim a real external business provider was exercised, and does not claim PF9/PF10 production authority integration before the canonical M10/R4 adapters exist.
