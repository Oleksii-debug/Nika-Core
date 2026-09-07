# PF8 Repeat-Incident Durability — 2026-08-23

Status: ONE-SHOT-16 current-main convergence implementation/evidence contract. This document does not grant merge or acceptance credit by itself.

## Problem closed by this batch

The earlier PF8 incident coordinator deduplicated every incident trigger fingerprint forever. That was correct for retries while an incident was active, but incorrect after a terminal rollback/resolution: a later recurrence of the same approved operational signal could be returned as the already-terminal incident instead of receiving an independent incident identity and maintenance lifecycle.

The original recurrence workstream (#178) closed the sequential/restart semantics. ONE-SHOT-16 preserves that design on current main and additionally closes the atomic check/create race where simultaneous same-fingerprint opens could otherwise create overlapping occurrences.

This batch makes deduplication occurrence-aware without weakening the existing repair, independent-review, regression, deployment, rollback or reconciliation authority checks.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE** the integrated `IncidentRepairReleaseCoordinator`, Product Operations evidence binding, PF2 trusted-plan/review authority, Toolsmith bounded-path/non-shell acceptance contracts, and PF3 deployment/rollback authority.
- **ADAPT** the existing fingerprint index from “one permanent incident per fingerprint” to “latest dedup target per fingerprint”.
- **CUSTOM (thin)** only Nika-specific recurrence timing, restart validation, schema-lineage rules, and a local `RLock` around the occurrence check/create critical section.
- No new dependency or generic incident-management framework is added.

## Runtime invariants

1. Same `incident_id` + same trigger fingerprint remains idempotent.
2. A fingerprint whose latest incident is non-terminal remains idempotent regardless of later observation time; a monitoring retry cannot fan out duplicate repair work.
3. A terminal incident remains the dedup target for delayed/stale observations at or before its terminal release/rollback evidence time.
4. A genuinely later observation may create a new incident only when its timestamp is strictly after the prior terminal release/rollback timestamp.
5. The new occurrence receives its own incident identity and therefore its own bounded work order, review, regression and release lifecycle.
6. The fingerprint index always points to the latest occurrence, never an older terminal record.
7. The same-fingerprint dedup lookup and new-occurrence insertion are serialized inside one coordinator process. Two concurrent opens for the same later trigger therefore converge on one winner occurrence instead of creating two overlapping active incidents.
8. No incident path writes production source directly. Repair remains bound to the existing isolated coding-worker/review/release authority chain.

## Durable schema lineage

The trigger fingerprint itself stays anchored to the historical v1 fingerprint schema so existing persisted fingerprints are not re-keyed after upgrade.

- readable legacy snapshot: `nika-pf3-incident-repair-release-v1`;
- current snapshot: `nika-pf3-incident-repair-release-v2`;
- fingerprint identity schema remains the v1 identity payload.

Legacy v1 snapshots are accepted under their original one-fingerprint-per-incident invariant. After they are restored into the current coordinator, the next `snapshot()` is written as v2. This is a lazy, deterministic migration: no trigger identity is regenerated and no prior incident history is discarded.

## v2 restart validation

For each shared fingerprint family, restore fails closed unless all of the following are true:

- every fingerprint index entry points to a real incident;
- every fingerprint family has exactly one index entry;
- the index target is the latest occurrence by trigger observation time;
- two occurrences cannot have the same observation time;
- every predecessor occurrence is terminal (`RESOLVED` or `ROLLED_BACK`);
- every successor observation is strictly later than its predecessor's terminal release/rollback evidence;
- existing candidate/review/deployment authority validation still passes for all persisted evidence.

The persistence entrypoint exposes the same authority contract as runtime restore: `review_authorities` is typed as `TrustedReviewAuthority`, not raw `CoordinatorSnapshot`. That keeps the public restart API aligned with the fail-closed host-provided trusted-plan fingerprint validation already enforced by `IncidentRepairReleaseCoordinator.restore()`.

This prevents persisted-state tampering from rewinding the current incident pointer, creating overlapping active repairs, inserting a recurrence before the previous repair lifecycle actually terminated, or encouraging callers to treat candidate-contained coordinator state as review authority.

## Concurrency evidence

`tests/test_product_factory_incident_recurrence.py` includes a deterministic race harness. It replaces only the in-memory fingerprint map in the test with an instrumented mapping that forces two unsynchronized callers to capture the same pre-write lookup value. The production `RLock` prevents that interleaving: both callers return the same new occurrence, only one new incident exists, the fingerprint index targets it, and the resulting snapshot restores cleanly.

This is in-process coordinator concurrency evidence. It is not represented as an inter-process distributed lock or as a durable provider-effect journal.

## Regression evidence added/preserved

The recurrence suite covers:

- persistence review-authority annotation matching the runtime `TrustedReviewAuthority` contract;
- active duplicate idempotency;
- stale terminal retry suppression;
- later repeated occurrence isolation;
- concurrent later-repeat atomicity;
- latest occurrence becoming the active dedup target;
- restart continuity of a terminal predecessor plus active recurrence;
- fail-closed fingerprint-index rewind;
- fail-closed recurrence chronology tampering;
- v1 read compatibility, stable fingerprint identity and lazy v2 write migration.

The pre-existing PF8 suites continue to cover operations-bound evidence, strict repair scope, trusted independent review, exact regression/artifact evidence, staging/production release authority, rollback, uncertain-release reconciliation, secret-free persistence and multi-service restart isolation.

## Explicit non-claims

- No real production deployment, cloud/provider action, SSH/WinRM action or credential use is performed by this batch.
- No deployment/promotion provider contract is modified.
- No automatic production self-modification is introduced.
- The local incident lock is not a substitute for an inter-process database transaction; cross-process durable Product Operations effect journaling remains a separate canonical-host concern where external side effects are involved.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- GREEN/INTEGRATED credit requires exact-current-head automated gates plus independent audit.
