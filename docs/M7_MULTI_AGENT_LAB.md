# M7 Multi-agent laboratory

Status: integrated baseline on `main`; AUTO03 durable-team-lifecycle successor candidate is isolated on `fix/agent-lab-durable-team-lifecycle` until exact-head acceptance evidence is green.

## Reuse decision
- **REUSE / ADAPT** the already integrated LangGraph runtime only through Nika `AgentRuntimePort`; no second orchestration kernel is introduced.
- **REUSE** Pydantic `ToolGrant` from the integrated Agent Builder contract rather than inventing a parallel permission schema.
- **REUSE** SQLite and the existing ordered migration runner for durable team lineage/evidence.
- **REUSE** the existing Nika `AuditLog` for team creation/spawn/cancellation/lifecycle evidence.
- **CUSTOM (thin)** team identity, typed handoffs, parent/child lineage, privilege attenuation, quotas, evaluator aggregation, cancellation propagation and explicit team-finalization policy because these are Nika product/safety semantics not owned by LangGraph.

The stale draft PR #11 was audited. Its useful bounded-delegation ideas were not merged wholesale because it predates the current integrated M6 Agent Builder contracts, does not use the canonical schema/runtime state, and is non-mergeable against current main.

## Contracts
`TeamQuota` bounds depth, children per parent, total agents and concurrent execution. `TeamMember` persists stable parent/child identity, agent definition version, runtime thread ID, attenuated tool grants, lifecycle state and optional resume token. `AgentHandoff` is a typed task/result/status/error message with explicit team, sender, recipient, handoff and correlation IDs.

Privilege attenuation is fail-closed. A child may only request tools already granted to the parent, may not request a higher risk tier, and scopes must be a subset of the parent's scopes. Escalation attempts are rejected instead of silently broadening permissions.

## Persistence and restart evidence
SQLite migration v6 remains the durable schema for team, member, handoff and result evidence; the lifecycle repair does not require a new migration. Supervisor-created child identity and its TASK handoff are committed together. Before `runtime.run()` is awaited, `RUNNING` state and a runtime-provided initial resume cursor are committed together. After execution returns, member state, result evidence and RESULT/ERROR handoff are committed as one SQLite transaction.

A runtime that advertises `DURABLE_RESUME` must expose a non-empty `initial_resume_token`; otherwise fan-out fails before any child is spawned. This prevents M7 from claiming crash-safe execution while persisting no pre-execution recovery cursor.

`recoverable_members()` remains the broad product-level view. `recoverable_children()` excludes the root supervisory identity. `MultiAgentSupervisor.recover_team()` starts persisted `SPAWNED` children from their durable TASK handoff and resumes persisted `RUNNING` children through `AgentRuntimePort.resume(RuntimeResumeRequest)`. `WAITING_APPROVAL` is deliberately not auto-resumed because restart recovery must never invent or bypass a human approval decision.

The M7 layer does not replace M2 checkpoints. LangGraph remains execution/checkpoint truth behind `AgentRuntimePort`; M7 stores only product-level team identity, lineage, policy, recovery routing and result evidence.

## Supervisor
`MultiAgentSupervisor` uses the existing `AgentRuntimePort` for child execution and recovery. Fan-out is bounded with an asyncio semaphore derived from the persisted team quota. One worker exception is contained and recorded as that child's failure while sibling results remain valid. Runtime result states are normalized into durable M7 member states.

Team cancellation first verifies that the team is still active, then calls the runtime cancellation port for recoverable members and marks the team/unfinished members cancelled in Nika persistence. A late runtime completion cannot overwrite an already-cancelled team/member. A completed or failed team is terminal and cannot later be reclassified as cancelled; that rejection occurs before runtime cancellation side effects. Repeating cancellation of an already-cancelled team is idempotent. Typed TASK and RESULT/ERROR handoffs are recorded around execution, with terminal result/state/handoff evidence committed atomically.

Team completion is **explicit**, not automatic after each fan-out wave. Auto-closing after one wave would make legal later/nested fan-out impossible. `finalize_team()` fails while any child is `spawned`, `running` or `waiting_approval`. Once all children are terminal, a team with at least one completed child is `completed` even if another child failed (failure containment); a team with failures and no completed child is `failed`; an all-cancelled active team becomes `cancelled`.

## Evaluator
Evaluator aggregation is deterministic arithmetic over typed `EvaluationScore` records. Scores must be finite. One aggregate operation represents exactly one metric; attempting to combine records from different metrics fails closed instead of silently averaging unlike quantities such as quality and latency. M7 does not use an LLM to decide whether a score exists or to invent a metric. More advanced learning/promotion policy belongs to M8.

## Acceptance evidence required
Before the durable lifecycle successor is integrated:
1. dependency consistency, Ruff, compile and complete pytest pass on the exact candidate;
2. Ubuntu and Windows shared CI both green on the same exact head;
3. process-loss after pre-execution cursor binding is recovered through `runtime.resume`, not replayed as a fresh child;
4. persisted-but-not-started `SPAWNED` work restarts from the persisted TASK payload;
5. `WAITING_APPROVAL` remains blocked after restart until an explicit approval path is invoked;
6. injected result-handoff failure rolls back result/state atomically;
7. late runtime completion after team cancellation cannot resurrect work;
8. mixed success/failure and all-failure team finalization policies are deterministic;
9. completed/failed teams reject later cancellation before runtime side effects;
10. activated-definition/grant/quota/cancellation/evaluator regressions remain green;
11. exact branch/SHA and CI evidence are recorded before merge.

`PACKAGED`, `HUMAN_TESTED` and `NVDA_VERIFIED` are separate later gates and are not claimed by this M7 successor.
