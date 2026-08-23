# M7 Multi-agent laboratory

Status: integrated M7 foundation on `main`; MANUAL-DEV21 successor PR #194 is a separate hardening
candidate and receives no integration credit until exact-head acceptance and coordinator merge.

## Reuse decision

- **REUSE / ADAPT** the integrated LangGraph runtime only through Nika `AgentRuntimePort`;
  no second orchestration kernel is introduced.
- **REUSE** Pydantic `ToolGrant`, canonical SQLite and the existing `AuditLog` public API.
- **CUSTOM (thin)** team identity, typed handoffs, parent/child lineage, privilege attenuation,
  quotas, evaluator aggregation, cancellation authority/reconciliation and finalization policy.

## Contracts and fan-out

`TeamQuota` bounds depth, children per parent, total agents and concurrent execution. `TeamMember`
persists parent/child identity, exact activated agent version, runtime thread, attenuated grants,
lifecycle state and optional resume token. `AgentHandoff` is typed TASK/RESULT/STATUS/ERROR evidence.

Privilege attenuation is fail-closed. Atomic fan-out admits the complete validated wave under one
SQLite writer transaction or admits none of it. Durable member/thread identity, remaining quota,
TASK handoffs and spawn audit evidence are decided before runtime execution begins.

## Persistence and restart

Core migration v6 remains the authoritative team/member/handoff/result schema. Cancellation cleanup
adds a versioned M7 extension schema in the same SQLite database, with its own future-version
fail-closed migration marker so DEV21 does not collide with the shared global migration stream.

Before `runtime.run()` is awaited, RUNNING state and the runtime-provided initial recovery cursor are
committed together. Result state, result evidence and RESULT/ERROR handoff commit atomically.
`recover_team()` starts persisted SPAWNED children from their TASK handoff and resumes RUNNING
children through `AgentRuntimePort.resume`. WAITING_APPROVAL is never auto-resumed.

## Durable cancellation

Cancellation authority commits before external runtime effects. One transaction records the stable
cancellation operation, exact member/task/thread effect set, team `CANCELLED` state and all current
nonterminal member `CANCELLED` states. Only after that commit may runtime cleanup begin.

`MultiAgentSupervisor.cancel_team()` is the sole production path allowed to create new cancellation
authority because only the supervisor owns both the durable journal and runtime cleanup port.
`MultiAgentStore.cancel_team()` remains as a compatibility guard: it may read an already-cancelled
team idempotently, but it refuses an ACTIVE-to-CANCELLED transition that would bypass the journal.
A legacy/local `CANCELLED` team with no journal is not accepted as proof of external cleanup. The
supervisor atomically adopts its cancelled member/task/thread identities as `RECONCILE_REQUIRED`
evidence; no runtime effect is issued until a read-only probe proves `NOT_CANCELLED`. `CANCELLED`
confirms the old effect and `UNKNOWN` remains fail-closed. This adoption uses the existing M7
extension schema and does not manufacture historical success or require a schema bump.

Each external effect follows `PLANNED -> DISPATCHING -> CONFIRMED`. Exception or process loss after
`DISPATCHING` is uncertain and cannot be replayed. It requires the optional read-only
`CancellationReconciliationPort`; `CANCELLED` confirms the old effect, `NOT_CANCELLED` permits one
exact retry, and `UNKNOWN` remains blocked. Confirmed effects are never repeated. An unfinished
cancellation also blocks team recovery, new fan-out and finalization.

This does not claim that every runtime can inspect an old cancellation effect. When no trustworthy
probe exists, uncertainty deliberately remains blocked rather than manufacturing success or
reissuing a potentially duplicate side effect.

## Finalization and evaluator

Team completion remains explicit. Finalization fails while child work is nonterminal or a durable
cancellation operation is unfinished. Mixed success/failure containment retains successful sibling
evidence; all-failure and all-cancelled outcomes remain deterministic.

Evaluator aggregation is deterministic arithmetic over finite `EvaluationScore` records. One
aggregate represents exactly one metric; mixed metric names fail closed. Promotion policy remains in
M8 and requires declared metrics and fixed/held-out evidence.

## Acceptance evidence required

The current successor must pass dependency consistency, Ruff, compile and complete pytest on one
exact candidate head on Ubuntu and Windows. Focused durability evidence includes atomic fan-out,
restart-safe execution, WAITING_APPROVAL blocking, result rollback, late completion after cancel,
exact AUD03 effect→exception reproduction, no blind cancel replay after restart/crash, reconciliation
verdict handling, concurrent cancel callers, direct-store authority bypass rejection, legacy
unjournaled cancellation adoption, completed-cancellation idempotency, and future M7 extension-schema
rejection.

`HUMAN_TESTED` and `NVDA_VERIFIED` remain false until their separate human protocols are executed.
