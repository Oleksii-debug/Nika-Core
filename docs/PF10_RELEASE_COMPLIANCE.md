# PF10 exact release compliance

## Scope

PF10 is an evidence and release-policy boundary. It does not make legal conclusions. A positive license, permission, legal-basis, or proprietary-reuse conclusion is accepted only when a trusted host authority validates the exact `(project_id, evidence_ref, purpose)` tuple. No integrated trusted review authority currently exists on canonical `main`, so the production positive path remains fail closed.

## Evidence chain

The release path is:

`dependency -> exact source/version -> license evidence/disposition -> provenance -> distribution obligations -> notice evidence -> trusted review evidence -> exact release snapshot -> compliance decision -> release grant -> exact PF9 delivery authorization`.

`DependencyAdoption` retains the existing PF10 project/component/package/version/source/provenance/license/obligation/review semantics. `ReleaseDependency` adds an exact SHA-256 source-artifact binding and explicit transitive component edges. Missing or non-SHA-256 source identity is rejected before evaluation.

`ReleaseComplianceSnapshot` binds the exact project, release identifier, project source ref and source SHA-256, delivery artifact ref and SHA-256, verified notice-bundle SHA-256, dependency graph, obligation evidence, dependency notice evidence, competitor/reuse evidence, dependency-closure review and scope-review reference. Its deterministic digest changes when any of those inputs change.

## Fail-closed attacks

The release layer rejects:

- duplicate component or canonical package identity;
- package aliases that resolve to conflicting versions or source digests;
- missing/invalid immutable source digest;
- unknown license markers;
- blocked or review-required license states from the base PF10 gate;
- missing transitive dependency identities;
- missing distribution-obligation evidence, including transitive components;
- missing, duplicate, orphan, cross-project, or package/version-mismatched notice evidence;
- fabricated or cross-project review/legal/permission evidence through the existing trusted-authority port;
- proprietary competitor evidence without separately trusted legal/reuse authority;
- dependency, project, artifact, release, or notice changes after a decision;
- caller-constructed or tampered positive decisions or grants;
- replay of a decision or grant against a different release snapshot;
- replay of a valid release-A grant as PF9 delivery/release B, even when the artifact reference is the same.

The decision/grant proof is process-local integrity evidence, not a human approval, legal opinion, durable signature, credential-store secret, or hostile-code sandbox.

## Restart rule

A serialized positive decision or grant is not durable authority. Process-local proofs intentionally do not survive process restart. After restart the host must reconstruct the current release snapshot and obtain a fresh PF10 evaluation against the current trusted review authority before issuing a new release grant. Persisted PF9 delivery records retain the exact release ID, artifact SHA-256 and PF10 snapshot digest as historical evidence, but those fields do not reconstruct or mint a fresh grant.

This separates durable evidence from current authority and prevents persisted `allowed=true` state from becoming replayable release authority.

## Packaging integration

`build_verified_notice_bundle()` directly reuses the canonical `nika_core.packaging.notices.build_third_party_notices()` generator and immediately reuses `verify_third_party_notices()`. The exact generated `THIRD_PARTY_NOTICES.txt` SHA-256 is then bound into `ReleaseComplianceSnapshot`.

`ProductReleaseComplianceGate.evaluate()` re-runs canonical notice verification and compares the live notice file digest with the snapshot. `require_release_allowed()` performs the same check again at release time, so notice mutation after evaluation invalidates the decision.

The dependency notice list and the packaging notice generator are intentionally separate evidence surfaces: PF10 requires explicit per-dependency notice evidence and rejects orphan/missing entries instead of assuming that a generated file alone proves dependency-graph completeness.

## Release / delivery authority boundary

`ReleaseComplianceGrant` is the exact-artifact release authority produced only after current-snapshot and current-packaging revalidation. It contains project, release, artifact, artifact SHA-256, snapshot digest, and evidence refs, protected by process-local integrity proof.

PF9 `BusinessFactory.record_delivery()` consumes only `ReleaseComplianceGrant`, not a generic compliance decision. Before any delivery authorization is persisted it fails closed unless:

- the grant is an authentic current-process grant;
- `grant.project_id` equals the linked ProductProject;
- `grant.release_id` equals the exact PF9 `delivery_id`/release identity;
- `grant.artifact_ref` equals the delivered artifact reference;
- artifact SHA-256 and PF10 snapshot digest are valid exact digests.

The separate trusted PF9 delivery authorization intent binds project ID, release ID, artifact ref, artifact SHA-256, PF10 snapshot digest, passing QA evidence and the PF10 evidence-reference set. The durable `DeliveryRecord` retains those exact release/artifact/snapshot bindings and restore recomputes the authorization fingerprint. A legacy delivery record lacking those exact bindings fails closed on restore rather than being upgraded by inference.

This does not make PF9 a legal authority and does not make PF10 production-positive while the canonical trusted review authority is unavailable.

## WorkOrder -> ProductProject precursor authority

Delivery compliance cannot repair an under-bound product request after build. PF9 therefore requires the trusted WorkOrder authorization to bind the normalized initial `ProductProjectSpec` SHA-256 before ProductProject creation. The handoff rejects a missing binding or same-WorkOrder substituted spec before the durable ProductProject effect. Stored ProductProject compliance metadata carries both the WorkOrder authorization fingerprint and product-spec fingerprint for restart/reconciliation checks.

Historical WorkOrders that predate this binding may deserialize for inspection, but they cannot create a ProductProject. A fresh trusted WorkOrder authorization is required; the runtime does not fabricate authority from the old scope string.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE existing `ProductComplianceGate`, `DependencyAdoption`, competitor/reuse evidence and authority port.
- REUSE canonical packaging notice generator and verifier.
- REUSE ProductProject initial-spec normalization/idempotency and PF9 trusted business-authorization intent machinery.
- REUSE standard-library SHA-256/HMAC for deterministic fingerprints and process-local tamper detection.
- ADAPT PF10 evidence into an exact immutable release snapshot and revalidation grant.
- ADAPT PF9 delivery authorization to carry the exact grant release/artifact/snapshot identity.
- CUSTOM(thin) only release identity, dependency-graph/notice cross-checks, snapshot digest, stale/replay detection, and exact handoff/delivery bindings.

No new dependency is introduced.

## Truth state

- No legal approval is fabricated or inferred.
- Trusted review authority unavailable/revoked/error -> fail closed.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- `PRODUCTION_RELEASE_READY=false`.
- Source/CI evidence can prove the authority contracts, but positive production release authority remains unavailable until a canonical trusted review-authority adapter is integrated and independently proven.
