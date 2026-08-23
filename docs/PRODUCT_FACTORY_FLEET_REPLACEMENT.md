# Product Factory fleet replacement durability

Status: PF3 replacement/rebalance/recovery implementation note for PR #165/current successor.
Updated: 2026-08-23.

## Scope

This surface owns provider-neutral replacement, rebalance and recovery of already-deployed fleet replicas. It reuses the integrated `ExecutionNodeRegistry`, deployment-fleet records, canonical `ReleaseRef`, and `ProductOperationsCoordinator`; it does not create another execution registry, deployment provider, credential broker, ProductProject repository, or maintenance coordinator.

No real deployment provider is required for acceptance. External mutation remains behind `ReplicaReplacementPort`.

## Exact replacement identity

Each dispatched mutation binds the exact:

- ProductProject/project id and fleet-plan id;
- environment id;
- service id and replica id;
- original deployment operation id and execution work id provenance;
- source node and selected target node;
- canonical release version, source SHA and artifact digest;
- authorization work id, independent review ref and replacement-plan fingerprint.

Release version is read from the canonical `DeploymentFleetPlan` `ReleaseRef`. The fleet summary environment/SHA/digest, exact deployment operation, and Product Operations service/release placement must agree before a provider call is allowed. Provider success is accepted only when target node, release version, source SHA, artifact digest and `healthy=true` match the exact request.

## Placement and disruption safety

Replacement preserves these fail-closed controls:

- cordon the source before target acquisition;
- block while unrelated source leases remain active;
- select targets through `ExecutionNodeRegistry.acquire`, including platform, features, toolchains, GPU and resource capacity;
- same-service replica anti-affinity;
- `max_replicas_per_node` across current and completed replacement placement;
- per-environment max-unavailable and max-concurrent disruption budgets;
- do not disrupt a healthy source below service `min_healthy_replicas`;
- allow an already-unavailable source to heal without consuming a second healthy-source disruption slot;
- block provider mutation while Product Operations reports revoked/blocked service credentials;
- retain a source cordon only when explicitly requested by the trusted replacement plan.

## Durable pre-effect boundary

`FleetReplacementDispatchJournal` is a narrow PF3 domain port. A production-capable replacement coordinator must receive a durable implementation; no journal means no provider `apply`.

Before `ReplicaReplacementPort.apply`:

1. target capacity is leased and the exact request is constructed;
2. the coordinator enters `DISPATCHING` in memory;
3. the durable journal synchronously commits exact request identity, attempt, source-cordon provenance and request checksum;
4. only an exact durable acknowledgement permits `apply`.

The current `SQLiteFleetReplacementDispatchJournal` stores only PF3 replacement recovery authority: canonical request payload, exact attempt/source-cordon provenance, request checksum, and optional exact terminal provider evidence/checksum. It does not store raw credentials.

If the process dies after durable intent but before/during/after the external effect, restart overlays even a stale coordinator snapshot from this journal and enters `RECONCILE_REQUIRED`. Recovery invokes `inspect`; it does not blindly call `apply` again. A durable terminal result can reconstruct the terminal replacement state without repeating the mutation. Corrupt, partial, conflicting, or scope-mismatched rows fail closed.

## Reuse decision after DEV16 integration

Current `main` now contains the generic runtime `IdempotencyLedger` and `RuntimeIdempotencyEffectJournal`; these were explicitly re-evaluated before retaining the PF3 journal.

**REUSE compatibility decision:** PF3 must not synthesize a runtime task merely to use that ledger. `IdempotencyLedger` is foreign-keyed to canonical runtime `tasks(task_id)`, while fleet-replacement authority is currently owned by a Product Factory replacement plan plus its Product Factory authorization work. Those identities are not a canonical runtime task today. Creating a shadow runtime task would introduce a second task/claim authority, contrary to the runtime ownership boundary documented by the integrated Deterministic Brain.

Therefore this batch keeps the PF3 journal as a domain-specific exact-effect recovery mapping, not as a new generic retry/idempotency framework. Once Product Factory replacement work has a canonical runtime-task binding, this adapter should be reconsidered so runtime ledger status/ownership can be reused without fabricating authority. No competing generic planner/runtime journal is introduced here.

## Restart and recovery

- Pending durable dispatch re-cordons its source during recovery.
- A surviving replacement work lease is validated against the exact target; a mismatched lease fails closed.
- A missing target lease does not authorize replay; inspection remains the only external recovery action.
- A pre-dispatch orphan replacement lease blocks until expiry before target reacquisition.
- Exact terminal provider evidence releases the replacement work lease and restores the normal terminal state without repeating provider mutation.

## Acceptance evidence in the lane

Focused tests cover:

- durable SQLite intent before provider effect;
- simulated hard process death after provider effect with a deliberately stale pre-dispatch coordinator snapshot, proving one `apply` followed by `inspect` only;
- stale-snapshot recovery from durable terminal provider evidence without reapply;
- corrupt durable request fail-closed behavior;
- uncertain and exception paths with inspection-only reconciliation;
- wrong release SHA and wrong release version rejection;
- live release provenance drift rejection on restore;
- source lease, capacity, credential and disruption-budget blocking;
- 60 services / 180 replacements across two environments with restart and partial node loss mid-wave.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.

## Shared-contract / dependency boundary

This lane does not import or cherry-pick unmerged PF6 #172, PF7 #162, PF8 #200 or PF12/trusted-plan #164 implementation. Their owners remain independent. In particular, this durability journal does not solve the existing trusted-plan authority audit finding: fleet replacement must consume the canonical independently anchored Product Factory authority after that authority is integrated, rather than inventing a second signature/trust scheme locally.
