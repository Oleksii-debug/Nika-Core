# PF10 exact release compliance

## Scope

PF10 is an evidence and release-policy boundary. It does not make legal conclusions. A positive license, permission, legal-basis, or proprietary-reuse conclusion is accepted only when a trusted host authority validates the exact `(project_id, evidence_ref, purpose)` tuple. No integrated trusted review authority currently exists on canonical `main`, so the production positive path remains fail closed.

## Evidence chain

The release path is:

`dependency -> exact source/version -> license evidence/disposition -> provenance -> distribution obligations -> notice evidence -> trusted review evidence -> exact release snapshot -> compliance decision -> release grant -> delivery authorization`.

`DependencyAdoption` retains the existing PF10 project/component/package/version/source/provenance/license/obligation/review semantics. `ReleaseDependency` adds an exact SHA-256 source-artifact binding and explicit transitive component edges. Missing or non-SHA-256 source identity is rejected before evaluation.

`ReleaseComplianceSnapshot` binds the exact project, release identifier, project source ref and source SHA-256, delivery artifact ref and SHA-256, verified notice-bundle SHA-256, dependency graph, obligation evidence, dependency notice evidence, competitor/reuse evidence, and scope review reference. Its deterministic digest changes when any of those inputs change.

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
- caller-constructed or tampered positive decisions;
- replay of a decision against a different release snapshot;
- use of a base `ProductComplianceDecision`, even when legitimately gate-issued, as delivery authority;
- release-grant project or artifact substitution at the Business Factory delivery boundary.

The decision/grant proof is process-local integrity evidence, not a human approval, legal opinion, durable signature, credential-store secret, or hostile-code sandbox.

## Restart rule

A serialized positive decision or grant is not durable authority. Process-local proofs intentionally do not survive process restart. After restart the host must reconstruct the current release snapshot and obtain a fresh PF10 evaluation plus release grant against the current trusted review authority. This prevents persisted `allowed=true` state from becoming replayable release authority.

## Packaging integration

`build_verified_notice_bundle()` directly reuses the canonical `nika_core.packaging.notices.build_third_party_notices()` generator and immediately reuses `verify_third_party_notices()`. The exact generated `THIRD_PARTY_NOTICES.txt` SHA-256 is then bound into `ReleaseComplianceSnapshot`.

`ProductReleaseComplianceGate.evaluate()` re-runs canonical notice verification and compares the live notice file digest with the snapshot. `require_release_allowed()` performs the same check again at release time, so notice mutation after evaluation invalidates the decision.

The dependency notice list and the packaging notice generator are intentionally separate evidence surfaces: PF10 requires explicit per-dependency notice evidence and rejects orphan/missing entries instead of assuming that a generated file alone proves dependency-graph completeness.

## Release / delivery authority boundary

`ReleaseComplianceGrant` is the exact-artifact release authority produced only after current-snapshot and current-packaging revalidation. It contains project, release, artifact, artifact SHA-256, snapshot digest, and evidence refs, protected by process-local integrity proof.

PF9 `BusinessFactory.record_delivery()` accepts only `ReleaseComplianceGrant`. It rejects a base `ProductComplianceDecision` even when that decision came from the base PF10 gate, and it independently requires the grant project identity and artifact reference to match the linked ProductProject and requested delivery artifact before the separate Business authorization intent is evaluated. The Business authorization fingerprint continues to bind the artifact, passing QA evidence and exact compliance evidence refs.

This closes the exact-release bypass without creating a second legal authority. Production-positive PF10 remains fail closed until a canonical trusted review-authority adapter is integrated; tests may use deterministic fake authorities only to prove contract semantics.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE existing `ProductComplianceGate`, `DependencyAdoption`, competitor/reuse evidence and authority port.
- REUSE canonical packaging notice generator and verifier.
- REUSE standard-library SHA-256/HMAC for deterministic fingerprints and process-local tamper detection.
- ADAPT PF10 evidence into an exact immutable release snapshot and revalidation grant.
- CUSTOM(thin) only release identity, dependency-graph/notice cross-checks, snapshot digest, stale/replay detection and the Business Factory grant-consumption compatibility boundary.

No new dependency is introduced.

## Truth state

- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- `PRODUCTION_RELEASE_READY=false`.
- Delivery now requires the exact release grant; production-positive release authority remains unavailable until a canonical trusted review-authority adapter is integrated and independently proven.
