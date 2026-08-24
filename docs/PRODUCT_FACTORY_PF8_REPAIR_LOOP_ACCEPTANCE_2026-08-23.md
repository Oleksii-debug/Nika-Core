# PF8 repair-loop acceptance vertical — 2026-08-23

Status: implementation candidate; exact-head Core CI and M12 evidence is required before GREEN credit.

## Scope and live baseline

ONE-SHOT-17 starts from live `main` `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba` and was dependency-aware resynced after live `main` advanced to `e8743566ffc673d6f8d272e88de0e027c23ab277`. The main advance was unrelated Deterministic Brain work; no Product Factory / Toolsmith / incident / deployment source changed in that resync.

This lane is intentionally additive. It does not replace Product Operations, ProductFactoryCoordinator, IncidentRepairReleaseCoordinator, CapabilityEscalationService, ProductFactoryProgramHost or DeploymentFabric.

Active ownership remains respected:
- PF8 lifecycle convergence: PR #286, successor to #178/#200 semantics;
- PF5 build execution: PR #177;
- PF6 deployment hardening: PR #172;
- PF12 checkpoint authority: PR #164.

## Reuse decision

- **REUSE** Product Operations service/observation state for healthy/degraded service evidence.
- **REUSE** `IncidentRepairReleaseCoordinator` for durable incident/work-order/candidate/release truth.
- **REUSE** `ProductFactoryCoordinator` plus `CodingWorkerComponentAdapter` for bounded component ownership, exact worker result evidence and independent review.
- **REUSE** `CapabilityEscalationService` + `ToolsmithRepository` + SQLite/CheckpointService for search/build/verify/register/restart state.
- **REUSE** canonical `TaskQueue` for the durable task row required by Toolsmith foreign-key authority; Product Factory does not write the `tasks` table directly.
- **REUSE** `DeploymentFabric` for staging-first deployment, health, rollback, uncertain reconciliation and restart snapshots.
- **REUSE** existing `ProductFactoryProgramHost` worker effect-ledger/checkpoint semantics; no second repair coordinator is introduced.
- **ADAPT (test boundary only)** deterministic local CodingWorker/Toolsmith/deployment provider fakes stand in only for external side effects. They make no network, account, credential or production deployment action.
- **CUSTOM (thin)** allow the ProductFactory Toolsmith bridge to accept Toolsmith's canonical initial `row_version=0` instead of rejecting the real repository result hidden by prior fake tests.
- **CUSTOM (thin)** exact-idempotent `TaskQueue.create_exact()` plus a host-task binding capability lets the PF bridge durably register its deterministic component task before Toolsmith references it. Conflicting identity/payload replay fails closed and does not create another task event.
- **CUSTOM (thin)** independent Toolsmith verification atomically pins the exact candidate digest; a different manifest digest is rejected before the lifecycle leaves `VERIFIED`.
- **CUSTOM (thin)** `release_ref_from_repair_build()` binds one independently accepted repair candidate to one exact successful `NormalizedBuildEvidence` SHA+artifact before constructing immutable `ReleaseRef`. It does not execute a build or deployment.

## Representative deterministic vertical

`healthy service -> degraded observation -> incident -> bounded repair work order -> durable PF component task -> Toolsmith capability gap -> reuse search -> isolated capability build -> independent capability verification -> exact capability registration -> bounded product CodingWorker -> independent Product Factory review -> exact build evidence -> immutable release identity -> staging -> health -> production -> health -> durable incident resolution -> restart/restore`

The acceptance test uses real production state machines and repositories on the inside. Only worker/provider effects are local fakes.

## Attack/evidence matrix

| Attack | Evidence in this batch | Expected truth |
|---|---|---|
| duplicate incident | same trigger fingerprint opened with another incident id | deduplicated to existing incident |
| duplicate deterministic child task | same PF component identity replayed through canonical `TaskQueue` | same task, one create event |
| conflicting child-task substitution | same task id with different immutable payload | fail closed, original task unchanged |
| duplicate deployment retry | same staging `DeploymentIntent` replayed | no second provider deploy call |
| worker failure | retryable process failure through real `CodingWorkerComponentAdapter` | Product Factory enters `REPAIR_REQUIRED`; no release authority |
| wrong-file repair | worker returns `outside/attack.py` | adapter rejects outside component allowed paths |
| stale review | `TrustedReviewAuthority` captured before independent review | candidate rejected; fresh exact authority required |
| Toolsmith post-review artifact substitution | verify digest A, then register manifest digest B | rejected while durable state remains `VERIFIED` with digest A |
| Toolsmith verification restart | restart after accepted verification and before registration | exact verified digest remains pinned; substitution still rejected |
| failed/stale build | failed build, wrong work id, wrong SHA or wrong artifact digest | no `ReleaseRef` produced |
| Toolsmith crash after external worker effect | durable `BUILDING`, simulated crash, process restart, `recover_build()` | recovery uses worker inspect/recover; no blind execute replay |
| deployment uncertainty | provider reports uncertain after deploy effect | durable `UNCERTAIN`; restart + provider inspection reconciles without duplicate apply |
| health mismatch | health evidence reports another release SHA | fail closed |
| rollback uncertainty/failure | unhealthy production and rollback cannot prove restoration | deployment is rejected; forged terminal incident closure rejected |
| incident restart | dump/load/restore with external review + deployment authority | exact resolved state restored and revalidated |
| Product Factory restart | restore with trusted plan fingerprint | exact accepted work state restored |
| Toolsmith restart | reopen canonical SQLite state | exact registered capability version/digest retained |
| DeploymentFabric restart | restore snapshot, replay same intent | idempotent record; no duplicate provider effect |

Broad regression authority is `scripts/verify.py`: dependency consistency, Ruff, compileall and full pytest. Core CI runs that verifier on Ubuntu and Windows. M12 remains an additional pre-human gate. Only checks for the exact final branch head count.

## Real-CI repair lineage

Exact head `5c28be938ca4d92c1a4fb76844fa31258f89f95a` reached real Core CI and M12 and was **RED**. Ubuntu `scripts/verify.py` completed dependency consistency, Ruff and compile successfully, then full pytest reported **3 failed / 1295 passed / 11 warnings**. All three failures were the same real integration defect: `ToolsmithRepository.create_escalation()` correctly enforced its foreign key to `tasks`, while the PF bridge derived a deterministic `component_task_id` that had never been registered in canonical task authority. Windows/M12 failed on the same candidate family; no green credit transfers from that head.

The repair does not weaken the foreign key and does not add test-only SQL. Canonical `TaskQueue` now has an exact-idempotent host-owned creation path, and the real `CapabilityEscalationService` exposes that path to the PF bridge through a narrow structural host-task binding capability. Fake escalation ports remain usable without durable task binding.

The same contract audit found a second integration defect before claiming acceptance: independent Toolsmith verification recorded the candidate digest only in transition evidence, so a later manifest could substitute a different artifact. Verification now durably pins the reviewed digest in the existing escalation row, and registration checks it before entering `REGISTERING`. No schema migration or parallel authority was added.

## Explicit non-credit / dependency boundaries

### PF5 build-executor uncertainty

The integrated baseline exposes `NormalizedBuildEvidence` but does not yet integrate the PF5 build execution fabric owned by PR #177. ONE-SHOT-17 therefore verifies exact accepted-repair -> build-evidence -> release lineage and rejects uncertain/failed/mismatched evidence, but does **not** claim executor-level crash-before/after-build-side-effect proof. That credit belongs after the canonical PF5 executor is integrated and can be consumed without stacking its branch.

### PF8 second ProductFactory attempt lineage

Current integrated PF8 review authority binds a `RepairWorkOrder.work_order_id`, base SHA and goal to one exact Product Factory work record. `ProductFactoryCoordinator.prepare_repair()` correctly creates a new attempt with a new work id and an exact new base/repair goal. The current PF8 authority cannot silently reinterpret that second attempt as the original work order.

This lane does not weaken the authority and does not fabricate lineage. The representative success path acquires/registers the missing Toolsmith capability before the single bounded outer Product Factory repair attempt; worker-failure retry is independently proven fail-closed. A future accepted PF8 convergence must define explicit versioned attempt lineage before a later Product Factory attempt may receive incident release authority.

### Legacy low-level Toolsmith transition compatibility

Existing trusted low-level repository tests can construct a historical `VERIFIED` row directly through `ToolsmithRepository.transition()` without a pinned digest. That compatibility surface is not treated as independent verification authority. The production `CapabilityEscalationService.accept_verification()` path now atomically pins the candidate digest and is the path credited by this acceptance vertical. A future Toolsmith contract-version migration may remove the legacy low-level shape after dependent tests/callers are migrated.

### External side effects

No real production provider, account, secret, credential, deployment, repository mutation or external network effect is exercised by this acceptance vertical.

## Acceptance truth

Until exact-head Core CI and M12 complete successfully:
- IMPLEMENTED: yes;
- GREEN: no;
- INTEGRATED: no;
- PACKAGED: no;
- HUMAN_TESTED: false;
- NVDA_VERIFIED: false;
- PRODUCTION_RELEASE_READY: false.