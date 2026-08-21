# PF3 Fleet / Node-Capacity Deployment Operations — 2026-08-21

## Scope

This successor extends the integrated PF3 execution-mediated deployment, wave and promotion stack with provider-neutral fleet placement semantics for independently deployable services with multiple replicas. It is additive: no manual DEV01–DEV05/M10 source, shared UI, provider playbook, credential-store implementation or release workflow is modified.

Starting technical base: `8e0fd432740f4087243f864b3dc297e03fcfd130`.

## REUSE → ADAPT → CUSTOM(thin)

REUSE:

- `ExecutionNodeRegistry` as the authoritative node/capability/resource matcher and one-active-lease-per-node allocator;
- `DeploymentExecutionCoordinator` for node leases, short-lived project-scoped credential authorization, provider mutation, node-loss/credential-block recovery and uncertain→inspect/reconcile behavior;
- `DeploymentFabric` for exact-release health, rollback and project+environment current-release integrity;
- existing wave/promotion coordinators for service ordering and STAGING→PRODUCTION lifecycle.

ADAPT:

- one independently deployable service becomes multiple durable `DeploymentExecutionSpec` replicas sharing exact project/environment/release identity but using unique operation/work/intent IDs;
- all runnable replicas enter the prepare/reservation phase before any provider completion, so real `ExecutionNodeRegistry` capacity semantics can spread active replicas across distinct nodes rather than immediately reusing one released node;
- a service declares `min_healthy_replicas`, yielding explicit HEALTHY/DEGRADED/BLOCKED/FAILED fleet state without rewriting healthy parallel replicas.

CUSTOM(thin):

- `ServiceFleetSpec`, `DeploymentFleetPlan` and `DeploymentFleetCoordinator` validate fleet topology, capacity-oriented execution ordering, summaries and restart binding to the underlying execution state.

## Fail-closed invariants

The fleet layer rejects:

- empty plan/service identity or empty replica sets;
- invalid `min_healthy_replicas`;
- duplicate service, operation, work or deployment-intent identities;
- replica drift in project, environment tier/provider identity or exact `ReleaseRef`;
- request/intent project mismatch;
- unknown, self, same-wave or later-wave dependencies;
- snapshot restore whose durable replica spec differs from the already-restored underlying execution operation.

The fleet snapshot intentionally does not serialize active execution-node leases, credential leases, protected-store handles or provider sessions. Those remain owned by the existing execution snapshot contract, where PREPARED state becomes `RECOVERY_REQUIRED` across restart.

## Capacity and failure semantics

`advance()` first prepares/retries every eligible replica and only then completes prepared provider operations. Because the integrated registry grants at most one active work lease per node, this creates an actual capacity reservation window. If a four-replica service has only three matching nodes, three replicas can become healthy while the fourth remains `WAITING_FOR_NODE`; with `min_healthy_replicas=3` the service is DEGRADED rather than globally failed. A later advance reacquires capacity and can restore HEALTHY state.

Node loss or credential revocation affects only the replica whose execution operation owns the ephemeral lease. Parallel healthy replicas and unrelated services retain their durable state. `RECONCILE_REQUIRED` uses the existing reconcile path and never issues a blind second completion/deploy call.

Dependencies gate only on a parent service reaching fully HEALTHY state. A failed parent blocks its declared dependents while unrelated services in later waves remain eligible.

## Deterministic qualification

Focused tests cover:

- exact-release drift rejection across replicas;
- duplicate work identity and invalid dependency topology;
- three-node capacity for a four-replica service, DEGRADED→HEALTHY recovery;
- simultaneous partial node loss and credential block with unrelated-service isolation;
- uncertain provider result reconciled without duplicate completion;
- dependency gating and failed-parent isolation;
- snapshot/restart exact-spec binding and duplicate snapshot-plan rejection;
- social/messenger-scale fixture with 60 independently deployable services × 3 replicas = 180 replica operations across three waves, snapshot/restart after wave 1, exact-SHA provenance per service and deterministic completion after restart.

This complements the already integrated 60-service/120-stage multi-environment promotion qualification rather than replacing it.

## Real vs fake boundary

The production fleet coordinator is real orchestration code over the public execution contract. Focused tests use a deterministic fake execution port to force node-capacity exhaustion, node loss, credential block, rejection and uncertain reconciliation without external infrastructure.

The integrated Windows credential implementation remains OS-backed Windows Credential Manager. The integrated optional Ansible staging bridge remains a production adapter boundary, but this batch does not invoke it.

No SSH, WinRM, Ansible remote run, cloud API call, remote host mutation, staging/production rollout or real credential use is performed by this qualification.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
