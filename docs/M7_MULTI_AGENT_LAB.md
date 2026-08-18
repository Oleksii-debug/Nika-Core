# M7 Multi-agent laboratory

Status: IMPLEMENTED candidate on `dev/m7-multi-agent-lab`; no milestone credit until exact Ubuntu + Windows CI is green and integrated.

## Reuse decision
- **REUSE / ADAPT** the already integrated LangGraph runtime only through Nika `AgentRuntimePort`; no second orchestration kernel is introduced.
- **REUSE** Pydantic `ToolGrant` from the integrated Agent Builder contract rather than inventing a parallel permission schema.
- **REUSE** SQLite and the existing ordered migration runner for durable team lineage/evidence.
- **REUSE** the existing Nika `AuditLog` for team creation/spawn/cancellation evidence.
- **CUSTOM (thin)** team identity, typed handoffs, parent/child lineage, privilege attenuation, quotas, evaluator aggregation and cancellation propagation because these are Nika product/safety semantics not owned by LangGraph.

The stale draft PR #11 was audited. Its useful bounded-delegation ideas were not merged wholesale because it predates the current integrated M6 Agent Builder contracts, does not use the canonical schema/runtime state, and is non-mergeable against current main.

## Contracts
`TeamQuota` bounds depth, children per parent, total agents and concurrent execution. `TeamMember` persists stable parent/child identity, agent definition version, runtime thread ID, attenuated tool grants, lifecycle state and optional resume token. `AgentHandoff` is a typed task/result/status/error message with explicit team, sender, recipient, handoff and correlation IDs.

Privilege attenuation is fail-closed. A child may only request tools already granted to the parent, may not request a higher risk tier, and scopes must be a subset of the parent's scopes. Escalation attempts are rejected instead of silently broadening permissions.

## Persistence and restart evidence
SQLite migration v6 adds durable team, member, handoff and result tables. Team/member lineage and execution identity are written before child runtime execution starts. `recoverable_members()` rehydrates spawned/running/waiting-approval members after process restart so the existing durable runtime/recovery layer has stable Nika-owned parent/child evidence to act on.

The M7 layer does not replace M2 checkpoints. LangGraph remains execution/checkpoint truth behind `AgentRuntimePort`; M7 stores only product-level team identity, lineage, policy and results.

## Supervisor
`MultiAgentSupervisor` uses the existing `AgentRuntimePort` for child execution. Fan-out is bounded with an asyncio semaphore derived from the persisted team quota. One worker exception is contained and recorded as that child's failure while sibling results remain valid. Runtime result states are normalized into durable M7 member states.

Team cancellation first calls the runtime cancellation port for recoverable members, then atomically marks the team and unfinished members cancelled in Nika persistence. Typed task and result/error handoffs are recorded around execution.

## Evaluator
Evaluator aggregation is deterministic arithmetic over typed `EvaluationScore` records. M7 does not use an LLM to decide whether a score exists or to invent a metric. More advanced learning/promotion policy belongs to M8.

## Acceptance evidence required
Before M7 can receive its 9% roadmap weight:
1. dependency consistency, Ruff, compile and complete pytest pass on exact candidate;
2. Ubuntu and Windows shared CI both green;
3. migration v6/restart evidence tests pass;
4. privilege-escalation and quota tests pass;
5. bounded parallel fan-out proof passes;
6. worker failure containment proof passes;
7. cancellation propagation proof passes;
8. exact branch/SHA and CI run are recorded in `state/PROJECT_STATUS.md` and Issue #1 before merge;
9. merge only the exact green candidate.

`PACKAGED`, `HUMAN_TESTED` and `NVDA_VERIFIED` are unrelated later gates and are not claimed by M7.
