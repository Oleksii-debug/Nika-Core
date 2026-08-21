# PF3 Execution-Mediated Staging Lifecycle — 2026-08-21

## Scope

This successor composes the already integrated PF3 `ExecutionNodeRegistry`, `CredentialBroker`, and `DeploymentFabric` instead of creating another provider or credential stack. It is provider-neutral and does not edit DEV01–DEV05, M10, shared UI, the Ansible adapter, Windows credential storage, or production release policy.

## Capability

A deployment operation now has a durable orchestration identity while node leases and credential leases remain process-ephemeral. The coordinator acquires a matching execution node, issues a project-scoped short-lived credential lease, authorizes credential use at prepare time and again immediately before the provider boundary, then calls the existing deployment fabric.

The lifecycle fails closed when the execution node disappears or the credential is revoked between prepare and execution. In both cases no provider mutation is attempted. An uncertain provider result becomes `RECONCILE_REQUIRED` and must use the integrated `DeploymentFabric.reconcile()` inspect path instead of replaying deploy blindly.

## Restart semantics

Coordinator snapshots never serialize node lease IDs, credential lease IDs, protected-store handles, or active node identity. A prepared operation is persisted as `RECOVERY_REQUIRED`. After restart it must reacquire both the execution-node lease and a fresh credential lease before any provider action. Completed/rejected/reconcile-required operations preserve deterministic durable state and evidence references.

## Scale evidence

Focused deterministic tests exercise 30 independently named services through one ProductProject and a shared staging environment, with the first half completed before snapshot and the remainder completed after restart. Additional tests cover node loss, credential revocation mid-operation, idempotent operation IDs, duplicate/corrupt snapshot rejection, and uncertain-provider inspect/reconcile without duplicate deploy calls.

This extends the already integrated 50-ProductProject shared-environment restart proof and Product Operations multi-service fixtures. It does not claim real distributed scheduling or a real provider execution target.

## REUSE → ADAPT → CUSTOM(thin)

- REUSE: `ExecutionNodeRegistry`, `CredentialBroker`, `DeploymentFabric`, exact project/environment/release identities, existing provider inspection/reconciliation.
- ADAPT: compose their public contracts into a durable operation state machine.
- CUSTOM(thin): operation state/snapshot and the minimal execution-node-health port needed to detect node loss before provider mutation.

No new dependency, cloud SDK, SSH/WinRM controller, custom vault, scheduler, database, or provider protocol is introduced.

## External-action truth

Tests use fake protected store, fake node-health observation, and fake deployment provider. No SSH, WinRM, Ansible remote execution, cloud API call, remote host mutation, staging mutation, production rollout, or real credential use is performed. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
