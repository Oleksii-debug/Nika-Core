# PF10 compliance hardening — 2026-08-23

Status: ONE-SHOT-19 implementation lane stacked on MANUAL-DEV30 PF9/PF10 PR #195.

## Exact dependency and ownership boundary

This lane started from PF10 head `14c0049a01c0cad85fba6a5b0184b16ed3247020` on branch
`work/manual-dev30/pf9-pf10-business-compliance`. PF9 remains owned by MANUAL-DEV30. This lane edits
only PF10 compliance semantics, the existing packaging-notice adapter surface, and focused tests.

M10 PRs #61/#62 are still unmerged. PF10 therefore does not import an unmerged approval service and
does not create a second signer. Positive license/legal/permission decisions remain fail-closed
behind
`ComplianceReviewAuthorityPort`. Opaque `review_ref` strings are evidence identifiers, not
authority.
A future composition root may adapt the integrated canonical M10 review verifier to this
port.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE the existing `nika_core.packaging.notices` generator and verifier. PF10 notice evidence is
  derived from the exact section produced by that generator; no second notice renderer exists.
- REUSE Python `importlib.metadata` through the existing packaging notice subsystem for the exact
  installed distribution name/version/license text used in the release bundle.
- REUSE SHA-256 for immutable source-byte commitments and canonical compliance-input identity.
- ADAPT the existing PF10 `ComplianceReviewAuthorityPort`, ProductProject/Business delivery path,
  and existing packaging release path.
- CUSTOM (thin) only Nika-specific dependency inventory, transitive parent binding, notice identity,
  compliance-input fingerprint and current-decision semantics.

No new third-party dependency is introduced.

## Exact compliance chain

PF10 now distinguishes the reviewed adoption record from the exact packaged inventory:

`DependencyAdoption -> PackagedDependencyEvidence -> obligations -> PackagingNoticeEvidence ->`
`trusted review evidence -> ProductComplianceDecision -> release/delivery gate`.

A release-allowing dependency requires:

- exact project/component/package identity;
- an exact version value rather than a version range;
- a source/provenance locator plus a canonical lowercase SHA-256 commitment for the adopted bytes;
- a non-empty recorded license expression that is not an explicit unresolved marker such as
  `UNKNOWN` or `NOASSERTION`;
- an authority-backed `APPROVED` disposition, or it remains blocked/review-required;
- all declared distribution obligations fulfilled;
- every declared notice reference resolved by exact package/version notice evidence;
- exact presence in the packaged dependency inventory, including reviewed transitive components;
- exact parent-component bindings so transitive graph drift and cycles fail closed.

The code does not infer legal compatibility from a license string. `LicenseDisposition.APPROVED`
remains a project-specific reviewed decision that must resolve through trusted authority.

## Packaging notice integration

`distribution_notice_record()` reuses the canonical `_distribution_section()` used by
`build_third_party_notices()`. It records the exact installed distribution name, normalized package
name, exact installed version, rendered notice body, and a SHA-256 notice reference derived from the
section title and body.

`build_pf10_notice_evidence()` is only an adapter from that canonical packaging record into PF10's
`PackagingNoticeEvidence`. A package/version mismatch, missing notice, duplicate notice reference,
or notice for an undeclared component blocks release.

## Stale decision and replay boundary

Every evaluation computes a canonical, order-independent input fingerprint over dependencies,
packaged inventory, obligations, notices, competitor evidence and scope-review reference. The
fingerprint is included in release evidence and in the process-local decision integrity proof.

For each project, only the latest evaluated proof remains current in that process. Re-evaluating a
changed dependency set invalidates an older positive decision. Tampering project/findings/evidence
or
input fingerprint invalidates it. A fresh process has a fresh integrity key and no current-decision
registration, so a prior in-memory positive proof fails closed after restart.

This mechanism is downstream anti-fabrication/staleness evidence only. It is not human approval,
legal review, durable signing authority, a secret store, or a hostile-code sandbox.

## Competitor/proprietary evidence boundary

Permitted-public evidence still requires exact trusted permission evidence. Proprietary material
requires separately trusted legal-basis and reuse-authorization evidence. Access to private source,
assets, credentials or a review-ref string never becomes copy permission by itself. Cross-project
review/evidence substitution fails closed.

## Acceptance truth

Focused tests cover exact positive evidence, source commitment, unresolved license/version
range, duplicate normalized dependency identity, transitive package omission, unreviewed packaged
extras,
orphan notices, stale decision after dependency change, decision tamper, fresh-process restart
rejection, and canonical packaging notice reuse. Full repository Ubuntu/Windows CI remains the exact
acceptance source after publication of the final branch head.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PRODUCTION_RELEASE_READY=false`
