# PF8 Product Operations — Maintenance Evidence Integrity

Status: ONE-SHOT-16 current-main convergence implementation/evidence contract.

## Collision and scope boundary

The original DEV10 maintenance/evidence workstream (#200) and repeat-incident workstream (#178) were non-overlapping at the file level but could not be merged directly: #200 had an active AUD02 authority block and both branches became stale after release-workflow/current-main convergence. ONE-SHOT-16 therefore preserves both semantics on one current-main convergence branch rather than stacking the stale PRs.

This maintenance slice owns Product Operations maintenance/evidence semantics and a thin adapter to the already-integrated runtime idempotency ledger. It does not own M10 approval issuance, deployment provider implementation, production promotion, credential storage, or a second persistence framework.

PF6 exact-release convergence remains a separate shared-contract dependency. Current-main PF8 release/rollback observations are still SHA-level and must not be silently rewritten against the unmerged PF6 #280 contract. When canonical PF6 exact `ReleaseRef` authority integrates, PF8 release/rollback validation requires an explicit compatibility migration and combined replay so same-SHA/different-artifact releases cannot alias.

## REUSE → ADAPT → CUSTOM (thin)

REUSE the existing `ProductOperationsCoordinator`, `ProductOperationsPort`, Product Operations snapshots, service observations, rollback evidence, maintenance request identity, canonical runtime `IdempotencyLedger`/SQLite store, and the project-wide rule that high-impact authority must be host-owned and fail closed.

ADAPT two existing host authorities behind PF8-owned framework-neutral ports:

- `MaintenanceApprovalAuthorityPort` consumes exact trusted approval authority. It receives the exact project, immutable `DeployableService` including release SHA, and complete `MaintenanceRequest`. It verifies host authority; it does not issue, sign, persist, or manufacture approvals.
- `MaintenanceEffectJournalPort` consumes durable pre-effect reservation/reconciliation authority. `RuntimeIdempotencyMaintenanceJournal` is the concrete thin adapter over the canonical runtime `IdempotencyLedger`; it requires an existing host `task_id` and never invents one.

CUSTOM is limited to exact evidence lineage, strict result identity, rollback sealing, occurrence/retry serialization, PF8 operation/fingerprint mapping and durable result schema normalization. No shell path, permission bypass, signer, HMAC key, approval database, second SQLite schema, provider-specific API, or self-modifying production mechanism is added.

## Approval and evidence invariants

- A non-empty caller-provided `approval_ref` is not positive authority.
- Maintenance requires both a configured side-effect port and a configured trusted approval verifier before provider dispatch.
- Missing verifier, verifier exception, or any verifier result other than literal `True` fails closed.
- The trusted verifier receives the exact project, service/environment/release identity, request id, action, reason, evidence refs and approval ref through immutable service/request objects.
- Before authority verification and before provider dispatch, every `MaintenanceRequest.evidence_refs` item must be present in the requested service's recorded health or rollback evidence.
- Cross-service or forged evidence is rejected before effect.
- The production regression derived from AUD02 #263 proves that `approval_ref="candidate-controlled:approved:R4"` cannot authorize a side effect when no trusted host verifier exists.
- Positive production maintenance remains dependent on an adapter to canonical integrated M10/R4 authority. Deterministic test resolvers prove only this consumer contract and do not become an approval issuer.

## Durable external-effect protocol

Every maintenance provider effect additionally requires `MaintenanceEffectJournalPort`. Missing journal blocks provider dispatch even when request evidence and approval verification are otherwise valid.

The integrated adapter reuses `IdempotencyLedger` with:

- operation type `product_operations.maintenance`;
- a stable operation key derived from exact `(project_id, request_id)`;
- an input fingerprint binding project, immutable service identity including release SHA/replicas/dependencies/credential references, and the complete maintenance request including action/evidence/approval reference;
- canonical host `task_id` supplied by the caller; SQLite foreign-key authority rejects a candidate-invented task identity;
- durable states `PENDING`, `UNCERTAIN`, `COMPLETED`;
- schema-versioned durable `MaintenanceResult` evidence for completed effects.

Ordering is deliberate:

1. exact request/service/approval evidence is validated;
2. the canonical SQLite ledger commits `PENDING` before any provider call;
3. only a newly-created reservation may execute `ProductOperationsPort.apply`;
4. an existing `PENDING` or `UNCERTAIN` reservation can only use `inspect`, never blindly replay `apply`;
5. a determinate result is committed to the ledger before Product Operations in-memory/snapshot state advances;
6. `COMPLETED` replay reconstructs the exact durable result with zero provider calls;
7. uncertain inspection remains unresolved; determinate inspection reconciles the existing durable reservation;
8. durable identity conflict or malformed durable result fails closed before provider mutation.

This is a reconciliation/idempotency protocol, not a false claim that SQLite and an external provider form one atomic transaction.

## Crash/restart boundaries

The maintenance external-effect crash window is now covered by the canonical runtime ledger rather than by `ProductOperationsSnapshot` itself:

- crash after reservation but before provider mutation leaves durable `PENDING`; restart inspects and never redispatches blindly;
- hard process loss after provider mutation but before local Product Operations save still leaves the pre-effect `PENDING`; restart inspects external state rather than invoking `apply` again;
- catchable provider failure/abrupt test loss marks the reservation `UNCERTAIN`; restart also inspects rather than replaying;
- provider success followed by a crash before local Product Operations save leaves durable `COMPLETED` result evidence; restart reconstructs the result without provider access;
- corrupt completed result JSON or semantic rebinding under the same operation identity fails closed;
- missing or candidate-created host task identity cannot reserve the canonical ledger and therefore cannot reach provider dispatch.

The ordinary in-memory Product Operations snapshot remains responsible for service/health/maintenance presentation state. The runtime ledger is the authoritative pre-effect/reconciliation record for external maintenance mutation. No second PF8 persistence authority is introduced.

## Runtime and concurrency invariants

- Request-id replay is exact and idempotent; a conflicting payload under the same request id is rejected by both Product Operations state and durable journal fingerprinting.
- `request_maintenance()` and uncertain-result reconciliation are serialized with an in-process `RLock`, so concurrent exact retries cannot race local state or double-dispatch within one coordinator process.
- The SQLite journal independently protects restart/cross-instance replay; in-process locking is not treated as crash durability.
- Service observation timestamps cannot move backwards; a different payload at the same timestamp is rejected rather than overwriting evidence.
- Exact rollback evidence replay is idempotent; conflicting rollback evidence is rejected.
- A terminal rollback seals the failed-release observation lineage: later observations for that failed release are rejected.
- Node-availability recomputation cannot resurrect a failed release after terminal rollback. Credential blocking may temporarily surface `BLOCKED`; when the credential is restored the service returns to rollback-derived terminal state rather than health derived from the failed release.
- Maintenance adapter apply/inspect results must cross the boundary as `MaintenanceResult`, with exact boolean flags and non-duplicate evidence references.

## Restart reconciliation of Product Operations snapshots

`restore()` validates the complete Product Operations snapshot before replacing coordinator state. It re-derives and checks:

- project/service identities and earlier-wave dependencies;
- revoked credential identity and each service's exact blocked-credential set;
- unavailable-node identity and exact per-service replica loss;
- service observation release/service/replica binding;
- rollback service/release/timeline binding;
- service health from durable observation, credential, node-loss and rollback evidence, with terminal rollback taking precedence once credential blocking clears;
- maintenance request uniqueness, target service, durable approval reference and exact service evidence binding;
- trusted host approval authority for each persisted maintenance request;
- maintenance state backed by persisted result evidence for that service.

Snapshot corruption therefore fails closed without partially replacing the coordinator's prior in-memory state. External-effect replay safety is separately anchored by the canonical runtime ledger as described above.

## Test evidence boundaries

Focused tests cover:

- AUD02 forged approval and exact action/service/release/request substitution;
- missing durable journal -> zero provider dispatch;
- concurrent exact retry -> one provider dispatch;
- real canonical `SQLiteStore` + real `TaskQueue` host identity + real `IdempotencyLedger` recreation;
- candidate-created/fake task identity rejected by canonical persistence authority;
- PENDING/UNCERTAIN restart -> inspection without re-apply;
- simulated provider effect followed by process loss -> one apply total across restart;
- COMPLETED-before-local-save -> exact durable result restoration with zero provider calls;
- rebound request/release identity conflict and corrupt result evidence fail closed;
- 50-service maintenance isolation and existing 60-service Product Operations isolation;
- rollback sealing, node/credential changes and late observation rejection.

`tests/pf8_effect_journal_fake.py` is intentionally only a deterministic unit-test contract fake. It is not cited as durability evidence. Durability credit comes from the SQLite/runtime-ledger integration tests.

## Truth

This is automated engineering evidence only. Exact candidate SHA, current-main compatibility, Core/M12 and independent audit status are recorded on the convergence PR; this document does not grant GREEN or integration credit by itself.

PF6 exact-release shared-contract compatibility remains pending until its canonical successor integrates and PF8 is replayed against that exact contract.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

No real production provider action is executed by this batch.