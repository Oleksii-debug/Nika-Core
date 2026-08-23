# PF8 Product Operations — Maintenance Evidence Integrity

Status: ONE-SHOT-16 current-main convergence implementation/evidence contract.

## Collision and scope boundary

The original DEV10 maintenance/evidence workstream (#200) and repeat-incident workstream (#178) were non-overlapping at the file level but could not be merged directly: #200 had an active AUD02 authority block and both branches became stale after release-workflow/current-main convergence. ONE-SHOT-16 therefore preserves both semantics on one current-main convergence branch rather than stacking the stale PRs.

This maintenance slice owns only Product Operations maintenance/evidence semantics. It does not own M10 approval issuance, deployment provider implementation, production promotion, credential storage, or a new persistence framework.

## REUSE → ADAPT → CUSTOM (thin)

REUSE the existing `ProductOperationsCoordinator`, `ProductOperationsPort`, durable snapshots, service observations, rollback evidence, maintenance request identity, and the project-wide rule that high-impact authority must be host-owned and fail closed.

ADAPT maintenance authorization through a framework-neutral consumer boundary, `MaintenanceApprovalAuthorityPort`. The port receives the exact project, immutable `DeployableService` including release SHA, and complete `MaintenanceRequest`. It verifies authority supplied by the trusted host; it does not issue, sign, persist, or manufacture approvals.

CUSTOM is limited to exact evidence-lineage validation, strict primitive/result identity checks, rollback sealing, restart reconciliation, and narrow in-process retry serialization. No shell path, permission bypass, signer, HMAC key, approval database, provider-specific API, or self-modifying production mechanism is added.

## Approval and evidence invariants

- A non-empty caller-provided `approval_ref` is not positive authority.
- Maintenance requires both a configured side-effect port and a configured trusted approval verifier before provider dispatch.
- Missing verifier, verifier exception, or any verifier result other than literal `True` fails closed.
- The trusted verifier receives the exact project, service/environment/release identity, request id, action, reason, evidence refs and approval ref through the immutable service/request objects.
- Before authority verification and before provider dispatch, every `MaintenanceRequest.evidence_refs` item must be present in the requested service's recorded health or rollback evidence.
- Cross-service or forged evidence is rejected before effect.
- The production regression derived from AUD02 #263 proves that `approval_ref="candidate-controlled:approved:R4"` cannot authorize a side effect when no trusted host verifier exists.
- Positive production maintenance remains dependent on an adapter to the canonical integrated M10/R4 authority. Deterministic test resolvers prove only this consumer contract and do not become an approval issuer.

## Runtime and concurrency invariants

- Request-id replay is exact and idempotent; a conflicting payload under the same request id is rejected.
- `request_maintenance()` and uncertain-result reconciliation are serialized with an in-process `RLock`, so concurrent exact retries cannot double-dispatch `ProductOperationsPort.apply`/`inspect` within one coordinator process.
- Service observation timestamps cannot move backwards; a different payload at the same timestamp is rejected rather than overwriting evidence.
- Exact rollback evidence replay is idempotent; conflicting rollback evidence is rejected.
- A terminal rollback seals the failed-release observation lineage: later observations for that failed release are rejected.
- Node-availability recomputation cannot resurrect a failed release after terminal rollback. Credential blocking may temporarily surface `BLOCKED`; when the credential is restored the service returns to the rollback-derived terminal state rather than health derived from the failed release.
- Maintenance adapter apply/inspect results must cross the boundary as `MaintenanceResult`, with exact boolean flags and non-duplicate evidence references.

## Restart reconciliation

`restore()` validates the complete snapshot before replacing coordinator state. It re-derives and checks:

- project/service identities and earlier-wave dependencies;
- revoked credential identity and each service's exact blocked-credential set;
- unavailable-node identity and exact per-service replica loss;
- service observation release/service/replica binding;
- rollback service/release/timeline binding;
- service health from durable observation, credential, node-loss and rollback evidence, with terminal rollback taking precedence once credential blocking clears;
- maintenance request uniqueness, target service, durable approval reference and exact service evidence binding;
- trusted host approval authority for each persisted maintenance request;
- maintenance state backed by persisted result evidence for that service.

Corruption therefore fails closed without partially replacing the coordinator's prior in-memory state.

## Crash truth

Current main does not expose a canonical durable pre-effect checkpoint host for `ProductOperationsSnapshot`. Therefore this convergence does **not** claim that an operating-system/process crash occurring after an external provider effect but before a host persists the resulting snapshot is fully reconciled.

Closing that boundary requires a compatibility-approved canonical checkpoint/task identity and durable effect journal or equivalent host, not a second ad-hoc PF8 database invented inside this lane. Until that host exists, positive production provider execution remains outside this acceptance claim.

## Scale and isolation evidence

Focused tests exercise 50 services with five independently maintained services and restart the snapshot, proving that maintenance state does not leak to the other 45 services. Existing 60-service Product Operations coverage remains the broad multi-service regression gate. Dedicated regressions cover terminal rollback plus node/credential changes, late observation rejection, trusted-approval substitution attacks, provider result typing and concurrent exact retry dispatch-once behavior.

## Truth

This is automated engineering evidence only. Exact candidate SHA and CI/audit status are recorded on the convergence PR; this document does not grant GREEN or integration credit by itself.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

No real production provider action is executed by this batch.
