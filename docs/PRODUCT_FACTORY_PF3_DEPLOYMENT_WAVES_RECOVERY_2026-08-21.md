# PF3 Deployment Waves and Long-Operation Recovery — 2026-08-21

## Scope

This PF3 successor layers deterministic multi-service rollout coordination on the integrated
`DeploymentExecutionCoordinator`. It does not replace ExecutionNode, CredentialBroker,
DeploymentFabric, Product Operations, the Windows protected-store adapter, or the authorized
Ansible staging adapter.

## Capability

`DeploymentWaveCoordinator` groups independently deployable services into staged waves while
preserving each service's durable deployment-operation identity. Dependencies must point to an
earlier wave. A service is advanced only after its declared dependencies have reached exact
`SUCCEEDED` state.

The coordinator delegates all provider-facing behavior to the existing execution-mediated
deployment lifecycle:

- PENDING -> prepare;
- WAITING_FOR_NODE / BLOCKED_CREDENTIAL / RECOVERY_REQUIRED -> retry and reacquire ephemeral
  execution-node and credential leases;
- PREPARED -> complete through the existing DeploymentFabric;
- RECONCILE_REQUIRED -> inspect/reconcile rather than blind replay.

No new provider protocol, scheduler, vault, SSH/WinRM controller, cloud SDK, or credential path is
introduced.

## Failure isolation

Rollout state is derived from service state, not from one shared mutable release flag. A failed or
rolled-back service produces `PARTIAL_FAILURE` without rewriting the durable state of parallel
healthy services. Later services are gated by their declared dependencies, so an unrelated service
may continue while a dependent service remains blocked.

Node loss and credential revocation remain service-scoped pause conditions. They do not cause
provider mutation while the underlying execution coordinator is blocked.

## Restart and corruption safety

A composite snapshot stores rollout records plus the existing execution snapshot. The rollout copy
is synchronized to the safe execution snapshot, so a PREPARED operation is persisted as
`RECOVERY_REQUIRED`; node lease IDs, credential lease IDs, protected-store handles, and active node
identity remain outside durable rollout state.

Restore fails closed on:

- duplicate rollout plan IDs;
- mismatched service/operation sets;
- missing execution state;
- rollout state, attempt count, or evidence refs that disagree with the execution snapshot.

After restart, recovery proceeds only through the integrated execution coordinator, which must
reacquire ephemeral execution-node and credential authorization before provider work.

## Scale evidence

Focused deterministic coverage uses a 60-service ProductProject arranged into three 20-service
waves on one staging environment. The fixture advances one wave, snapshots/restarts, then advances
the remaining waves while preserving dependency order and independent service state.

Additional coverage proves:

- duplicate and conflicting plan protection;
- invalid cross-wave dependency rejection;
- parallel healthy service preservation when one peer rolls back;
- node-loss and credential-block pause behavior;
- uncertain provider reconciliation without a second complete/deploy call;
- snapshot corruption rejection.

This extends the integrated 50-ProductProject restart proof and the 30-service execution-mediated
staging proof.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE: `DeploymentExecutionCoordinator`, `DeploymentExecutionSnapshot`, exact operation and
  project identities.
- ADAPT: deterministic wave/dependency scheduling and composite restart validation.
- CUSTOM(thin): rollout-plan and rollout-summary records only.

## External-action truth

Focused tests use a fake execution coordinator. No SSH, WinRM, Ansible remote execution, cloud API
call, remote/staging host mutation, production rollout, or real credential use is performed.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
