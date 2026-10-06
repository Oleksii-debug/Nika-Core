# PF3 Multi-Environment Exact-Release Promotions — 2026-08-21

## Scope

This PF3 successor layers an exact-release promotion lifecycle on the integrated deployment-wave and execution-mediated staging stack. It is additive and provider-neutral. It does not add a cloud SDK, SSH/WinRM controller, deployment scheduler, credential vault, database, or background agent.

The intended product shape is a complex ProductProject with many independently deployable services. Each service can be promoted through a shared staging environment and then a production environment while retaining exact source-SHA and artifact-digest identity. Cross-service dependencies are explicit promotion-batch dependencies rather than implicit global ordering.

## REUSE → ADAPT → CUSTOM(thin)

REUSE:

- `DeploymentExecutionCoordinator` for execution-node acquisition, short-lived project-scoped credential authorization, provider mutation, uncertain-state reconciliation, restart recovery and evidence refs;
- `DeploymentWaveCoordinator` for deterministic wave ordering, dependency gating, partial failure isolation and durable restart snapshots;
- `DeploymentFabric` for exact-SHA staging-first production enforcement, provider health evidence, rollback and current-release state.

ADAPT:

- a two-stage service promotion (`STAGING` then `PRODUCTION`) becomes two deterministic wave services;
- production always depends on the same service's successful staging stage;
- cross-service dependencies point to the dependency service's successful production stage in an earlier promotion batch.

CUSTOM(thin):

- `ServicePromotionSpec` and `DeploymentPromotionPlan` validate exact release identity and promotion topology;
- `DeploymentPromotionCoordinator` presents service/environment summaries and durable promotion snapshots without duplicating the underlying execution state machine.

## Fail-closed invariants

A service promotion is rejected before execution when:

- staging does not target `EnvironmentTier.STAGING`;
- production does not target `EnvironmentTier.PRODUCTION`;
- staging and production target the same environment identity;
- operation IDs or deployment-intent IDs are reused across the two stages;
- production release identity differs from staging in project/version/source SHA/artifact digest;
- a service depends on itself, an unknown service, or a service in the same/later promotion batch;
- operation or deployment-intent identity collides anywhere in the promotion plan.

Restore is rejected when the durable promotion plans do not deterministically reproduce the exact wave plans stored in the wave snapshot. Execution leases, credential leases, protected-store handles and active node identity remain owned by the underlying execution snapshot contract and are not added to the promotion snapshot.

## Failure isolation and recovery

A failed or rolled-back staging stage prevents only that service's production stage from running. Parallel services in the same promotion batch can remain healthy and continue to production. A `WAITING_FOR_NODE`, `BLOCKED_CREDENTIAL` or `RECOVERY_REQUIRED` execution reuses the wave/execution retry path. `RECONCILE_REQUIRED` reuses inspect/reconcile rather than issuing a blind second provider mutation.

The promotion layer does not weaken `DeploymentFabric` staging-first enforcement. Even if a malformed caller attempted to bypass promotion sequencing, production deployment still requires healthy staging proof for the exact release SHA in the underlying fabric.

## Deterministic qualification

Focused coverage includes:

- exact release SHA/digest mismatch rejection across staging and production;
- stage tier and identity validation;
- cross-service promotion dependencies restricted to earlier batches;
- staging-before-production ordering;
- parallel staging failure with unrelated production continuing;
- credential-block pause/retry without skipping staging;
- uncertain production reconciliation without duplicate completion;
- snapshot/restart between staging and production with exact release identity preserved;
- corrupted promotion/wave snapshot disagreement rejected fail-closed;
- 60 independently deployable services across three promotion batches and two environments, yielding 120 deterministic stage operations with snapshot/restart mid-promotion.

## Real vs fake boundary

The production promotion coordinator is real orchestration code over existing public PF3 contracts. Focused tests use a fake execution coordinator behind the real `DeploymentWaveCoordinator`; therefore they do not mutate a real host or provider.

The integrated Windows protected-store implementation remains the OS-backed Windows Credential Manager adapter. The integrated optional Ansible staging bridge remains a production adapter boundary, but this successor does not invoke it.

No SSH, WinRM, Ansible remote execution, cloud API call, remote/staging host mutation, production rollout, or real credential use is performed by this qualification. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
