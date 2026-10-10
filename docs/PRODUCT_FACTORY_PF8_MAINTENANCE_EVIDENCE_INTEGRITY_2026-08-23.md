# PF8 Product Operations — Maintenance Evidence Integrity

Status: ONE-SHOT-16 current-main convergence implementation/evidence contract.

## Scope and ownership

This lane converges the non-overlapping repeat-incident semantics from historical #178 and maintenance/evidence semantics from historical #200 on one current-main PF8 branch. It owns Product Operations maintenance/incident evidence plus thin adapters to already-integrated Nika authorities. It does not own M10 approval issuance, PF6 deployment implementation, provider-specific execution, credential storage, replica-placement authority, or a second persistence framework.

Current PF6 integration vehicle is #390. Its exact `ReleaseRef` contract is not yet canonical main and is therefore not imported speculatively. When PF6 exact-release authority integrates, PF8 must explicitly migrate/replay rollback validation so same-SHA/different-artifact releases cannot alias.

## REUSE → ADAPT → CUSTOM (thin)

REUSE:
- `ProductOperationsCoordinator`, Product Operations snapshots and existing provider port;
- canonical runtime `IdempotencyLedger`, `SQLiteStore` and `TaskQueue` host identity;
- existing service/health/rollback evidence;
- project-wide fail-closed approval/authority rules.

ADAPT:
- `MaintenanceApprovalAuthorityPort` consumes exact host-owned approval authority without issuing it;
- `MaintenanceEffectJournalPort` consumes durable pre-effect/reconciliation authority;
- `RuntimeIdempotencyMaintenanceJournal` is a thin adapter over canonical `IdempotencyLedger` and requires an existing host `task_id`.

CUSTOM(thin) is limited to PF8 operation identity/fingerprint/result mapping, evidence-lineage validation, rollback sealing, occurrence/retry serialization and snapshot↔journal consistency checks. No new dependency, DB/schema, signer, HMAC authority, generic scheduler, permission system or provider framework is introduced.

## Approval and evidence invariants

- Caller-provided `approval_ref` is evidence/reference only, never positive authority.
- Maintenance requires a configured provider port, exact service evidence and a configured trusted approval verifier.
- Missing verifier, verifier exception or anything other than literal `True` fails closed before provider mutation.
- Approval verification binds exact project + immutable service/environment/release + full request/action/reason/evidence/approval reference.
- Cross-service, stale-release and forged evidence is rejected before the effect.
- Positive production maintenance remains dependent on an integrated canonical M10/R4 adapter; deterministic test resolvers prove only the consumer contract.

## Durable effect journal

Every external maintenance effect also requires `MaintenanceEffectJournalPort`. The runtime adapter stores one stable operation under canonical SQLite:
- operation type `product_operations.maintenance`;
- operation key derived from exact `(project_id, request_id)`;
- fingerprint over complete project/service/release/request/evidence/approval subject;
- canonical host `task_id`, enforced by the existing task foreign key;
- states `PENDING`, `UNCERTAIN`, `COMPLETED`;
- schema-versioned durable `MaintenanceResult` for `COMPLETED`.

The journal exposes non-mutating `lookup(...)` as well as reserve/complete/uncertain/reconcile operations. Lookup independently verifies task identity, operation type and input fingerprint and never creates authority.

## Exact dispatch/recovery ordering

1. Validate service evidence and trusted approval.
2. Commit canonical `PENDING` before provider execution.
3. Only the caller that **created** that reservation may call `apply`.
4. A determinate result is committed as `COMPLETED` before local Product Operations state advances.
5. A returned uncertain result or catchable/abrupt provider failure is durably marked `UNCERTAIN`.
6. Existing `COMPLETED` reconstructs the exact durable result with zero provider calls.
7. Existing `UNCERTAIN` may use read/reconcile `inspect`; determinate inspection closes it, uncertain inspection leaves it unresolved.
8. Existing `PENDING` is **not** inspected and is never replayed by an ordinary request. It fails closed until a trusted host can prove the prior effect owner is gone and explicitly transition the operation to recovery authority.
9. `reconcile(PENDING)` is prohibited; reconciliation accepts only `UNCERTAIN`.

The distinction between `PENDING` and `UNCERTAIN` is deliberate. A second coordinator/process may observe another live owner while that owner is still inside `apply`; allowing immediate inspection would let the second process prematurely close the journal. The deterministic two-coordinator regression holds the first provider call open and proves the second exact request performs neither `apply` nor `inspect`.

This protocol provides fail-closed replay/reconciliation; it does not claim SQLite and an external provider form one atomic transaction.

## Crash/restart truth

Proven:
- reservation survives adapter/process recreation;
- provider exception/process-loss test after the external effect leaves `UNCERTAIN`, then restart reconciles through `inspect` without a second `apply`;
- `COMPLETED` before local state save restores the exact durable result without provider access;
- corrupt result JSON, rebound request/release identity and fake host task identity fail closed;
- raw hard-loss `PENDING` survives restart and blocks both dispatch and inspection until host owner-loss authority exists;
- an explicitly host-marked `UNCERTAIN` operation can then reconcile inspection-only.

Not claimed:
- generic cross-process owner-death/lease proof is not created by PF8;
- `PENDING` is therefore safe but may require external host recovery coordination for liveness.

## Snapshot ↔ journal authority

Independent QA #398 found that the earlier candidate accepted maintenance-bearing `ProductOperationsSnapshot` state without proving the same result in the canonical effect journal. A valid approval could therefore launder a fabricated applied/result state while the journal was missing, unresolved or held a different completed result.

Current repair makes journal authority mandatory on every maintenance-bearing restore and existing-record fast path:
- missing journal operation → reject;
- `PENDING` or `UNCERTAIN` while snapshot claims resolved state → reject;
- `COMPLETED` with different `MaintenanceResult` → reject;
- exact `COMPLETED` + exact result → accept;
- validation is non-mutating: failed restore cannot create/complete/reconcile an effect record;
- local existing uncertain state is valid only while the journal is exactly `UNCERTAIN`;
- local existing resolved state must equal exact durable `COMPLETED` evidence.

The production regression `tests/test_product_factory_operations_snapshot_journal.py` reproduces the #398 attack family using real `SQLiteStore`, real `TaskQueue` and real `IdempotencyLedger`, including a positive exact-result control.

## Other restart/data-integrity invariants

`restore()` still validates the full snapshot before replacing coordinator state:
- project/service identities and dependency waves;
- revoked credentials and derived blocked sets;
- unavailable nodes and derived replica loss;
- service observation release/service/replica identity;
- rollback service/release/timeline identity;
- health derived from durable observation/credential/node/rollback evidence;
- maintenance request uniqueness, service evidence and trusted approval authority;
- maintenance state derived from its exact result;
- exact journal result authority as described above.

Corruption therefore fails closed without partially replacing prior in-memory state.

## Test evidence

Focused coverage includes:
- AUD02 forged approval and action/service/release/request substitution;
- missing journal → zero provider dispatch;
- in-process exact retry → one provider dispatch;
- cross-coordinator live-`PENDING` race → second caller performs zero provider access;
- canonical SQLite/TaskQueue/IdempotencyLedger recreation;
- fake host task rejection;
- exception-after-effect → `UNCERTAIN` → inspection-only reconciliation;
- raw hard-loss `PENDING` fail-closed and explicit host-marked-uncertain recovery;
- `COMPLETED` before local save → zero-provider reconstruction;
- snapshot↔journal missing/unresolved/conflicting-result laundering attacks and exact positive control;
- corrupt durable result and rebound identity rejection;
- 50-service maintenance restart isolation and existing 60-service Product Operations isolation;
- rollback sealing, node/credential changes and late observation rejection.

`tests/pf8_effect_journal_fake.py` remains a unit-contract fake and is never durability evidence.

## Truth

Exact SHA/current-main compatibility, Core/M12 and independent auditor classification live on PR #286. This document does not grant GREEN or integration credit.

PF6 #390 exact-release compatibility, canonical M10/R4 positive authority, and wider PF3 replica-placement convergence remain separate shared-contract dependencies and are not silently absorbed here.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

No real production provider action is executed by this batch.