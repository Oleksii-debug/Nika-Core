# Product Factory fleet replacement durability

Status: PF3 implementation note for PR #165/current successor.
Updated: 2026-08-23.

## Scope

This surface owns provider-neutral replacement, rebalance and recovery of already-deployed
fleet replicas. It reuses the integrated `ExecutionNodeRegistry`, deployment-fleet records,
`ReleaseRef`, and `ProductOperationsCoordinator`; it does not create a second execution
registry, operations service, deployment provider, credential broker, or ProductProject
checkpoint system.

No real deployment provider is required for acceptance. Provider calls remain behind
`ReplicaReplacementPort`.

## Exact identity

Every replacement binds the exact:

- ProductProject/project id;
- fleet plan id;
- environment id;
- service id and replica id;
- original deployment operation id;
- source and selected target node ids;
- release version, source SHA and artifact digest;
- authorization work id, independent review evidence and replacement-plan fingerprint.

The release version is read from the canonical `DeploymentFleetPlan` `ReleaseRef`; the
fleet summary SHA/digest and Product Operations release SHA must agree before dispatch.
Provider success is accepted only when target node, release version, source SHA, artifact
digest and healthy status all match the exact request.

## Placement and disruption safety

Replacement preserves the current PF3 behavior:

- source node is cordoned before target acquisition;
- unrelated active source leases block replacement;
- target selection reuses `ExecutionNodeRegistry.acquire`, including platform, capability,
  toolchain, GPU and resource matching;
- anti-affinity excludes nodes already hosting a sibling replica of the same service;
- `max_replicas_per_node` is enforced against current plus completed replacement placement;
- per-environment max-unavailable and max-concurrent budgets are enforced;
- a healthy source is not disrupted below service `min_healthy_replicas`;
- an already unavailable source may be recovered without consuming another healthy-source
  disruption slot;
- blocked/revoked service credentials prevent the provider effect;
- retained source cordons remain disabled when explicitly requested.

## Durable pre-effect boundary

The external-effect crash window is closed by `FleetReplacementDispatchJournal`.
Production-capable execution must provide a journal; no journal means no provider `apply`.

Before `ReplicaReplacementPort.apply`:

1. target capacity is leased and the exact request is created;
2. the coordinator enters `DISPATCHING` in memory;
3. the journal synchronously commits the exact request id, plan/project/service/replica
   scope, attempt, source-cordon provenance and request checksum;
4. only an exact durable acknowledgement permits `apply`.

`SQLiteFleetReplacementDispatchJournal` is the thin canonical adapter over `SQLiteStore`.
It uses a PF3-owned extension table and canonical JSON/checksums; it does not use pickle or
persist raw credentials.

If the process dies after the durable intent commit but before/during/after provider effect,
a stale coordinator snapshot is overlaid from the durable journal on restart and becomes
`RECONCILE_REQUIRED`. Recovery calls `inspect` only. It never repeats `apply` blindly.

Terminal provider evidence is also committed to the journal before the in-memory record is
made terminal. Therefore a process loss after a confirmed result but before an external
snapshot can reconstruct the exact terminal state without repeating the effect. Corrupt,
conflicting, partially persisted or scope-mismatched journal rows fail closed.

## Restart and lease behavior

A pending durable dispatch re-cordons its source on recovery. A matching surviving target
lease is validated against the exact target; a missing lease does not authorize replay and
inspection remains the only recovery action. Pre-dispatch orphan replacement leases still
block until expiry before a new target can be acquired.

## Acceptance evidence

Focused automated coverage includes:

- durable SQLite intent before provider effect;
- simulated hard process death after the provider effect with a deliberately stale
  coordinator snapshot, proving one `apply` followed by `inspect` only;
- terminal durable evidence recovering a stale snapshot without reapply;
- corrupted durable request fail-closed behavior;
- uncertain and exception paths with inspection-only reconciliation;
- wrong release SHA and wrong release version rejection;
- release/version drift on restore;
- source lease, capacity, credential and disruption-budget blocking;
- 60 services / 180 replacements across two environments with restart and partial node
  loss mid-wave.

Automated tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.

## Compatibility / dependency decision

This batch does not import or cherry-pick unmerged PF6, PF7, PF8 or PF12 implementation
branches. Their current contracts remain independent. In particular, this durability
journal is not a substitute for the canonical trusted-plan authority work: fleet
replacement continues to consume the currently integrated review contract and must remain
gated by any unresolved independent authority audit until its canonical upstream authority
is integrated.
