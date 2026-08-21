# PF3 Fleet Maintenance / Node Drain / Rolling Recovery — 2026-08-21

## Scope

This batch adds a provider-neutral rolling fleet-maintenance coordinator on top of the
already integrated PF3 deployment fleet, Product Operations health model, and
ExecutionNodeRegistry. It does not add a second scheduler, vault, cloud API, SSH/WinRM
control plane, or provider-specific maintenance implementation.

REUSE -> ADAPT -> CUSTOM(thin):

- REUSE `DeploymentFleetRecord` for exact release SHA + artifact-digest provenance.
- REUSE `ProductOperationsCoordinator` for durable replica-to-node topology, health,
  credential blocking, node-loss aggregation, and restart snapshots.
- REUSE `ExecutionNodeRegistry.enabled` to cordon a node before destructive work so new
  execution leases cannot be acquired there.
- CUSTOM(thin) `NodeMaintenancePort` is the only external side-effect boundary for
  drain/restart/verify/resume. Every request carries explicit approval, project/fleet/node
  identity, exact service release provenance, affected replica identities, and evidence refs.

## Safety and recovery invariants

1. Maintenance is project scoped and bound to one existing fleet plan.
2. Target execution nodes must exist and be enabled when a new plan takes ownership.
3. Only one active rolling-maintenance plan may own a node in one coordinator.
4. Fleet and Product Operations must agree on service/project/environment/release SHA.
5. Artifact digest is taken from the exact fleet service record and carried to the side-effect
   request.
6. Before drain, every affected service must retain at least `min_healthy_replicas` after
   removing the target node. Existing node loss is included in that calculation.
7. Credential revocation blocks the next external maintenance action without calling the port.
8. Cordon is local deterministic state: `ExecutionNode.enabled=False`. It happens before drain
   and remains durable through the ExecutionNodeRegistry snapshot.
9. A pre-existing active work lease blocks drain. The node stays cordoned so no new lease can
   replace the one being waited on.
10. External `uncertain` never triggers blind replay. The exact durable request is retained and
    only `inspect()` may reconcile it.
11. Uncertain drain is treated conservatively as node unavailable until inspection proves
    otherwise.
12. Verification must prove exactly every drained replica identity healthy before resume.
13. Resume re-enables Product Operations node availability and ExecutionNode acquisition only
    after the external resume result is confirmed applied.
14. Restart snapshot contains plans, bindings, checkpoint/action state and evidence refs only.
    It contains no credentials, protected-store handles, provider sessions or active lease
    secrets.
15. Restore fail-closes if live fleet/topology/release bindings drift or if an in-progress
    maintenance record no longer matches the durable execution-node cordon state.

## Deterministic qualification

Focused tests cover:

- cordon -> drain -> restart -> exact-replica verify -> resume;
- active work lease blocking with no drain side effect;
- quorum blocking before cordon;
- credential revocation mid-maintenance and later recovery;
- uncertain drain -> inspect/reconcile with exactly one apply call;
- incomplete verification fail-closed;
- restart with execution-node cordon parity and corrupt parity rejection;
- fleet release/topology drift rejection before another side effect;
- 60 independently deployable services x 3 replicas = 180 replicas across a rolling
  three-node maintenance plan, with snapshot/restart after the first node.

The scale fixture keeps `min_healthy_replicas=2` for three-replica services, so one node may
be drained while unrelated services and replicas remain healthy. Each node is fully resumed
before the next node starts.

## Real vs fake

`RollingFleetMaintenanceCoordinator` is production orchestration code. The focused tests use
a deterministic fake `NodeMaintenancePort`; they do not contact a real host/provider. Existing
Windows protected credential storage remains the separate OS-backed implementation already
integrated on main. Existing optional Ansible staging support is not invoked by this batch.

## External actions not performed

No SSH, WinRM, Ansible remote execution, cloud/provider API call, remote host mutation,
staging deployment, production deployment, production-account action, or real credential use
is performed by this batch or its tests.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PRODUCTION_RELEASE_READY=false`
