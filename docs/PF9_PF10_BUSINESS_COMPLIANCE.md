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
- Nika's existing `SQLiteStore.connection()` transaction boundary is reused for durable PF9 snapshots.
- Existing packaging notice generation/verification remains responsible for Nika runtime bundle notices. PF10 project compliance records dependency-specific notice/obligation evidence instead of replacing that packaging subsystem.

### ADAPT

- Research evidence IDs are constrained to the exact objective research package before an opportunity can be created.
- ProductProject handoff is bound to an authorized WorkOrder and durable ProductProject identity.
- PF10 dependency records add the project-level source/version/license/provenance and distribution-obligation decision needed before delivery.

### CUSTOM (thin)

Nika-specific custom code is limited to the Business lifecycle state machine, policy authority boundaries, restart-safe snapshot persistence, append-only ordered audit evidence, and PF10 release/delivery compliance decisions.

## Policy and authority boundaries

`BusinessPolicy` is fail-closed:

- Lead creation is allowed only on declared channel IDs.
- Communication may be draft-only, separately approved, or covered by an explicit standing-policy reference.
- Contract authority remains approval-required. The PF9 contract has no autonomous contract-signing mode.
- Financial authority is record-only. PF9 stores invoice/payment state supplied by an external evidence reference and has no payment executor.
- Proposal approval and WorkOrder authorization are separate evidence references.
- Delivery requires a separate authorization reference, passing QA, the exact linked ProductProject identity, and an allowed PF10 compliance decision.

These contracts do not grant a provider/account credential and do not bypass Nika's R0-R4 approval/security boundary.

## PF10 compliance model

`DependencyAdoption` requires exact non-empty:

- project/component identity;
- package name and version;
- source reference;
- provenance reference;
- recorded license expression;
- license disposition from an authorized review/policy process.

The gate does not infer whether a license is acceptable. `REVIEW_REQUIRED` and `BLOCKED` remain release-blocking until an authorized process records an approved disposition.

Every declared distribution obligation must have matching fulfillment evidence. A component marked as requiring notices must carry notice references. Missing/unacceptable provenance, license disposition, obligations, or notice evidence blocks the compliance decision and therefore blocks PF9 delivery.

Competitor evidence is permitted only when it is recorded as permitted public evidence, or when proprietary material carries both a legal-basis reference and an explicit reuse-authorization reference. Possession or access alone is not treated as copying permission.

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

## Acceptance evidence in this lane

Focused regressions cover:

- complete Business flow into the real durable ProductProject repository;
- SQLite restart and continuation;
- unknown research evidence rejection;
- unapproved channel rejection;
- qualification/approval ordering;
- contract and money authority non-expansion;
- QA and exact-project PF10 delivery gate;
- forged snapshot and stale-writer rejection;
- missing dependency version/provenance rejection;
- blocked/review-required license rejection;
- missing notices and distribution fulfillment;
- unpermissioned competitor evidence and proprietary-copy authorization boundaries.

Exact-head CI remains the acceptance evidence source. This document does not assign `HUMAN_TESTED` or `NVDA_VERIFIED` and does not claim that any real external business provider has been exercised.
