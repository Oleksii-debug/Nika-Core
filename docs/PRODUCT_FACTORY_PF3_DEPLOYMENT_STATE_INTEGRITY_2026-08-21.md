# PF3 Deployment State Integrity — 2026-08-21

## Scope

This PF3 successor hardens the already integrated ExecutionNode / DeploymentFabric / credential / Product Operations / authorized-staging foundation. It does not add a second provider stack and does not edit DEV01–DEV05 or M10 source.

## Root-cause family

The integrated deployment fabric used only `environment_id` as the current-release key. Two ProductProjects could therefore both use an ordinary environment name such as `shared-staging` and accidentally inherit each other's previous release. That could corrupt rollback provenance and violated the ProductProject isolation required for large multi-product operation.

The same state boundary also accepted several malformed restart snapshots that the independent PF4 adversarial matrix correctly rejects: multiple active execution leases on one node, invalid lease time ordering, empty environment/provider identity, current-release state not backed by a healthy deployment record, and a claimed successful rollback that did not restore the recorded previous SHA.

## Repair

- current release identity is now `(project_id, environment_id)`;
- snapshots emit explicit `(project_id, environment_id, source_sha)` current-release entries;
- restart remains backward-compatible with the old two-field current-release shape only when one and only one healthy record proves the project identity;
- ambiguous legacy state fails closed instead of guessing;
- `EnvironmentIdentity` rejects empty environment/project/provider fields;
- ExecutionNode restore rejects two active leases on one node, empty lease identities, naive timestamps, and expiry-before-issue state;
- DeploymentFabric restore validates record semantics, exact health/rollback identity, staging proof backing, current-release backing, duplicate state, and rollback restoration identity;
- runtime rollback success must restore the exact recorded previous SHA;
- no raw credentials, provider stdout, remote host data, or external side effects are persisted by this change.

## Deterministic evidence

Focused tests cover:

- two projects sharing the same staging environment name without state collision;
- rollback isolation across projects;
- exact previous-release preservation across restart;
- rejection of forged/unbacked current-release state;
- safe migration of an unambiguous legacy snapshot and rejection of an ambiguous one;
- uncertain deployment reconciliation after restart without corrupting another project;
- 50 independent ProductProjects sharing one environment name across snapshot/restart;
- duplicate active execution leases and invalid lease lifetime rejection;
- empty environment/project/provider identity rejection.

These tests overlap the currently open PF4 adversarial acceptance findings so PF3 fixes the production owner lane rather than weakening or modifying PF4 tests.

## REUSE / ADAPT / CUSTOM(thin)

REUSE the integrated `ExecutionNodeRegistry`, `DeploymentFabric`, `DeploymentProviderPort`, exact-SHA evidence contracts and snapshot model. ADAPT only state-keying and restore validation. CUSTOM(thin) consists of normalization/validation helpers for project-scoped deployment state. No new dependency, database, provider SDK, SSH controller, cloud control plane or credential system is introduced.

## External-action truth

No real SSH, WinRM, Ansible control-node run, cloud API call, staging mutation, production deployment or real credential use is part of this batch. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
