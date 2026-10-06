# M2 crash-consistency boundary review

Updated: 2026-08-18.
Status: IMPLEMENTED/PREPARED on M2 branch; executable CI evidence still required, so no integration credit.

## Purpose
Review persistence boundaries in the prepared M2 durable runtime and eliminate process-loss windows that could create unrecoverable or unsafe state before M2 integration.

## Finding 1 — durable start crash window
The previous `TaskRuntimeCoordinator.start()` committed `READY -> RUNNING` before committing the Nika task -> runtime/thread cursor. A process loss between those writes could leave stale RUNNING durable work with no Nika-owned recovery pointer.

### Remediation implemented
The M2 branch now uses one caller-owned SQLite transaction for durable runtime start:
1. validate and persist the task `READY -> RUNNING` transition and task event;
2. insert the initial ACTIVE runtime-session cursor in the same transaction;
3. commit both together or roll both back together.

`TaskQueue.transition_with_connection()` and `RuntimeSessionStore.record_active_with_connection()` are intentionally small transaction-aware primitives. They preserve the normal public APIs while allowing this Nika-owned atomic product boundary.

Fresh `start()` no longer UPSERTs an existing runtime cursor. A task that already owns persisted recovery state fails closed and must use an explicit resume path. This prevents a new invocation from destroying crash-recovery ownership.

Audit rows are evidence, not recovery truth: recovery decisions depend on task/session/idempotency state. Audit append failure therefore cannot be used as authority to reopen or replay work.

## Finding 2 — result finalization remains conservative
For terminal outcomes `_finish()` transitions the task before deleting the runtime-session pointer. A failure between those operations may leave a terminal task plus stale session, but startup recovery must classify that pair as inconsistent and never reopen the terminal task automatically.

For resumable outcomes the runtime result is persisted before task-state transition; mismatched pairs likewise fail closed during startup classification.

## Fault-injection proofs prepared
`tests/test_runtime_crash_consistency.py` now prepares deterministic checks for:
- injected failure immediately after ACTIVE cursor insertion rolls back both the task transition and cursor, and the runtime is never invoked;
- a fresh start cannot overwrite an existing recovery cursor and the attempted READY -> RUNNING transition is rolled back;
- injected terminal session-delete failure leaves the task terminal while startup recovery classifies the stale pointer as `INCONSISTENT_STATE`, never `AUTO_RESUME_CRASH`.

Existing crash/recovery, approval and idempotency suites remain part of the same executable gate.

## Remaining executable gate
Before PR #3 may be integrated, the exact rebased SHA must actually execute and pass:
1. Ruff and compile checks;
2. full `.[dev,agent]` pytest;
3. real LangGraph + async SQLite restart/resume/corrupt-checkpoint/cancellation proofs;
4. the new atomic-start and finalization fault-injection proofs;
5. approval/idempotency/startup-recovery regression suites.

M2 remains IMPLEMENTED/PREPARED, not INTEGRATED, until that evidence exists.