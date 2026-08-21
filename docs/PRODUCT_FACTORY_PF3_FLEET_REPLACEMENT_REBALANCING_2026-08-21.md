# PF3 Fleet Replacement / Rebalancing + Multi-Environment Budgets — 2026-08-21

## Status

Candidate implementation for AUTO-PF3 Deploy Ops. This document records implementation and
qualification intent only. Exact-head GitHub Core CI + M12 remain authoritative acceptance gates.

- Starting live `main`: `449ed6dc34e8911aa2759b2a1219fd2720d11dd8`.
- `IMPLEMENTED=true` only after the five-file candidate is committed to its PF3 branch.
- `GREEN=false` until exact-head Core CI and M12 both succeed.
- `INTEGRATED=false` until an expected-head merge after a final live-main compatibility check.
- `HUMAN_TESTED=false`.
- `NVDA_VERIFIED=false`.
- `PRODUCTION_RELEASE_READY=false`.

## Ownership and scope

This batch is confined to AUTO-PF3-owned Deployment / Execution Node / Product Operations
coordination. It does not edit DEV01 Research/Corpus, DEV02 Toolsmith/CodingWorker, DEV03 Trader,
DEV04 Interaction/UIA, DEV05 Media, M10 authorization, shared UI, release workflows, provider
playbooks, credential-store implementation, or any real deployment account.

Additive candidate paths:

1. `src/nika_core/product_factory_fleet_replacement.py`
2. `tests/pf3_fleet_replacement_support.py`
3. `tests/test_product_factory_fleet_replacement.py`
4. `tests/test_product_factory_fleet_replacement_recovery.py`
5. `docs/PRODUCT_FACTORY_PF3_FLEET_REPLACEMENT_REBALANCING_2026-08-21.md`

No new dependency, migration, scheduler, credential store, SSH implementation, cloud SDK, provider
SDK, or control plane is introduced.

## REUSE → ADAPT → CUSTOM(thin)

### REUSE

The coordinator reuses the integrated public PF3 contracts:

- `ExecutionNodeRegistry` for deterministic platform/capability/resource matching, enabled/cordoned
  node state, one active lease per node, lease expiry, snapshot and restore;
- `DeploymentFleetRecord` for exact project/environment/release and deployment-operation provenance;
- `ProductOperationsCoordinator` for service topology, replica-to-node placement, health,
  `min_healthy_replicas`, node loss, and credential revocation/block state;
- existing exact `ExecutionRequest` resource/capability constraints;
- existing SHA-40 and artifact-digest provenance carried by the integrated fleet.

### ADAPT

The integrated node/fleet/operations state is adapted into a replacement lifecycle that can:

- cordon a source node before selecting a target;
- refuse a provider side effect while another active work lease still owns the source node;
- reacquire capacity after lease expiry/restart without serializing ephemeral authority into the
  durable replacement snapshot;
- place replicas across heterogeneous nodes while respecting capability/resource requirements,
  service anti-affinity, and an explicit maximum replicas-per-node placement cap;
- enforce per-environment disruption and concurrency budgets;
- preserve minimum healthy replica quorum while relocating a healthy source;
- allow non-worsening healing from a source node that is already unavailable;
- re-check credential and exact live placement/release provenance immediately before external work.

### CUSTOM(thin)

Only Nika-specific durable replacement semantics are custom:

- exact replacement plan/request/binding identity;
- deterministic replacement state machine;
- environment disruption/concurrency budget accounting;
- relocation placement overlay for already completed replacements;
- exact provider evidence validation;
- snapshot/restart validation and uncertain-result reconciliation.

The module does not implement transport, SSH/WinRM, provider login, cloud node provisioning, or a
new deployment scheduler.

## Durable lifecycle

Each replacement binds exactly:

- ProductProject identity;
- fleet plan identity;
- environment identity;
- service identity;
- replica identity;
- original deployment operation identity;
- source execution node;
- exact source SHA;
- exact artifact digest;
- replacement execution request;
- explicit approval reference and evidence references.

The provider request additionally binds the selected target execution node. A provider success is
accepted only when evidence proves the exact requested target node, source SHA, artifact digest,
and healthy result.

Durable states distinguish:

- pending work;
- source active-lease blocking;
- orphan replacement-lease blocking;
- target-capacity blocking;
- environment-budget blocking;
- credential blocking;
- pre-provider dispatch checkpoint;
- reconcile-required uncertainty;
- success;
- failure.

## Capacity and placement invariants

Target selection remains owned by `ExecutionNodeRegistry.acquire()` for platform, features,
toolchains, GPU requirement and resource-envelope matching. The replacement layer narrows eligible
nodes before calling that existing allocator:

- source node is excluded;
- a node already hosting another replica of the same service is excluded;
- a node at or above `max_replicas_per_node` is excluded;
- disabled/cordoned nodes remain ineligible through the registry itself.

The exclusion shim is synchronous and restores temporary registry eligibility immediately after the
single acquire call. It is not an external provider cordon and does not mutate remote infrastructure.
The coordinator is intentionally a serialized stateful orchestration component, consistent with the
existing PF3 coordinator family.

Completed replacements create a durable placement overlay so later replacements in the same plan
account for the new target placements rather than repeatedly using the pre-plan topology.

## Multi-environment maintenance budgets

Every environment touched by a plan requires exactly one `EnvironmentReplacementBudget`.
Uncovered or extra environments fail closed at submission/restore.

Each budget carries:

- `max_unavailable_replicas`: maximum additional unavailable replicas across that environment;
- `max_concurrent_replacements`: maximum provider-dispatched/reconcile-required replacements in
  that environment.

A healthy source replacement is rejected before provider work when it would either exceed the
explicit environment unavailable budget or reduce its service below `min_healthy_replicas`.

A replica whose source node is already recorded unavailable may be relocated even if the environment
is already above its normal disruption budget, because relocation is non-worsening healing rather
than a newly introduced outage. Exact credential and provenance checks still apply.

## Lease and restart semantics

Replacement work leases remain owned by `ExecutionNodeRegistry`; they are not copied into the
replacement snapshot. The durable snapshot contains plans, exact bindings, state, evidence,
selected target provenance and exact pending provider request.

Before a provider call, the coordinator stores a `DISPATCHING` checkpoint containing that exact
provider request. If the call raises or returns `uncertain`, the durable state becomes
`RECONCILE_REQUIRED`. Restart converts any persisted `DISPATCHING` state to
`RECONCILE_REQUIRED`; only `ReplicaReplacementPort.inspect()` may then resolve the outcome.
The provider `apply()` call is never blindly replayed.

If a matching replacement work lease survives registry restart/recovery:

- an unexpired lease blocks reacquisition as `WAITING_FOR_ORPHAN_LEASE`;
- after exact expiry, the existing registry expiry path removes it during a new acquire and the
  replacement may deterministically reacquire capacity.

Restore rejects duplicate plan/record identities, altered replacement scope/order, exact live
fleet/operations provenance drift, duplicate matching work leases, and a pending work lease whose
target does not match the durable pending provider request.

Terminal replacement leases are cleaned only by exact `project_id + work_id` identity.

## Credential safety

The coordinator consumes only Product Operations credential-block state. A revoked credential blocks
the affected replica before provider work and permits continuation only after the integrated owner
restores that credential state.

No raw credential, credential lease, protected-store handle, token, cookie, password, provider
session, or secret value appears in a replacement plan, snapshot, evidence document or test fixture.

## Failure isolation

A rejected replacement becomes `FAILED`, but unrelated replacements remain independently runnable.
The overall plan reports `PARTIAL_FAILURE` when at least one replica failed while other work may have
succeeded. A failed or blocked replica does not rewrite the durable state of healthy unrelated
services/environments.

Provider evidence with the wrong target node, SHA, artifact digest, or health result is rejected
fail-closed rather than promoted to success.

## Deterministic focused qualification

The focused suite contains 16 scenarios covering:

1. exact source→different-target replacement and source-cordon restoration;
2. intentional retained source cordon;
3. heterogeneous target resource/capability matching;
4. per-node placement cap plus same-service anti-affinity;
5. capability/capacity drift with zero provider side effect until capacity returns;
6. environment disruption-budget blocking;
7. healing from an already unavailable source without worsening disruption;
8. credential revocation before provider work and explicit recovery;
9. active source execution lease blocking;
10. orphan replacement lease waiting through exact expiry and deterministic reacquisition;
11. uncertain provider result → inspect without a second apply;
12. provider exception → exact durable pending request → restart → inspect only;
13. exact target/SHA/digest/health success validation;
14. failed-replica isolation from unrelated service replacement;
15. restart rejection for live release/source placement drift;
16. scale/restart/node-loss qualification.

### Scale fixture

The large deterministic fixture creates:

- 60 independently deployable services;
- 3 replicas per service;
- 180 replacement operations;
- 2 production environments (`prod-eu`, `prod-us`);
- 12 heterogeneous-capable execution nodes;
- snapshot/restart after 45 completed replacements;
- partial node loss before the remaining relocation wave;
- exact SHA/digest provenance for every service.

All 180 replacements must finish with unique durable replacement work identities and no leaked active
replacement work leases.

## Local authoring preflight

The authoring runtime cannot resolve `github.com` for a network checkout and does not provide the
repository Ruff binary. Therefore no local repository Ruff/full-suite GREEN is claimed.

Completed before publication:

- production module Python `py_compile`: PASS;
- focused test/support modules Python `py_compile`: PASS;
- maximum source/test line length <= 100: PASS;
- contract-compatible semantic harness using the current public PF3 contract shapes: **16/16 PASS**.

The semantic harness is development evidence only. It is not a substitute for the repository's exact
candidate checkout, dependency consistency, Ruff, compileall, full pytest suite, Windows Core job,
or M12 packaged-release path.

## Exact acceptance required

The branch may integrate only if all of the following are true on one exact candidate SHA:

1. Core CI succeeds on Ubuntu and Windows;
2. M12 Pre-Human Release Gate succeeds on that same SHA;
3. dedicated PF3 Windows Credential Store proof is either successful when in scope or correctly
   skipped when this additive batch does not touch protected-storage paths;
4. live `main` is re-read after CI;
5. branch/main compare proves no incompatible drift or foreign-lane overlap;
6. merge uses an expected-head SHA guard.

## External-action truth

No SSH, WinRM, real Ansible remote run, cloud/provider API call, remote-host mutation, staging or
production deployment, production-account action, or real credential use is part of this batch.
Focused qualification uses a deterministic fake replacement provider behind the real production
port contract.
