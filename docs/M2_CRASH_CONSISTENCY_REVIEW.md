# M2 crash-consistency boundary review

Updated: 2026-08-18.
Status: source-review evidence only; no integration credit.

## Purpose

Review every persistence boundary in the prepared M2 durable runtime while hosted CI is blocked. The goal is to find process-loss windows that could produce an unrecoverable or unsafe product state before M2 is allowed to integrate.

## Finding 1 — start ordering has an unsafe recovery window

Current `TaskRuntimeCoordinator.start()` performs these durable product operations in this order:

1. transition Nika task from `READY` to `RUNNING`;
2. append `runtime.started` audit event;
3. bind the Nika `RuntimeSessionStore` task -> runtime/thread/resume-token pointer;
4. await the runtime invocation.

For durable runtimes such as LangGraph, step 3 is the product cursor that lets a recreated Nika process locate the framework-owned checkpoint/thread.

If the process disappears after step 1 but before step 3, the database can contain a stale `RUNNING` task with no `runtime_sessions` row. `RuntimeRecoveryService` inventories persisted runtime sessions, so this task has no durable cursor to classify or resume. This violates the intended durable-runtime gate even if LangGraph itself would later have been capable of checkpointing.

### Required remediation before M2 integration

Do not merely hide this window with a startup heuristic. The start boundary must become fail-closed and crash-consistent.

Preferred design:

- add a small Nika-owned transaction boundary that can persist the task state transition and the initial durable session pointer in one SQLite transaction when the runtime exposes an initial durable cursor;
- keep audit append either in the same transaction where practical or make recovery truth depend on task/session state, never on the audit row;
- reject a new `start()` when the task already owns a persisted runtime session; callers must use an explicit resume path instead of overwriting recovery state;
- non-durable runtimes may continue without a durable session row, but the capability difference must be explicit and must not be represented as durable recovery.

A simple reorder (session first, task second) is safer than the current order but still leaves a stale-session window if the process dies before the task transition. The integration-quality fix should therefore prefer a shared transaction over two independently committed writes.

## Finding 2 — result finalization is conservative but needs fault-injection proof

Current `_finish()` records resumable session state before transitioning a task to `WAITING_APPROVAL`, `PAUSED` or `FAILED`. For terminal/non-resumable outcomes it transitions the task before deleting the session pointer.

These orderings are generally fail-closed: a crash between the two operations tends to leave an inconsistent task/session pair that startup recovery will refuse to auto-resume rather than blindly replaying work. However, this statement must be proved with fault injection on the exact implementation, not accepted from source inspection alone.

Required proof cases:

- crash after storing resumable result but before task-state transition -> startup classification is `INCONSISTENT_STATE`, never auto-resume;
- crash after terminal task transition but before session deletion -> terminal task is never reopened;
- failure while deleting a terminal session cannot make a completed/cancelled task automatically resumable;
- failure while appending runtime audit does not change recovery truth.

## Finding 3 — approval boundary is correctly fail-closed in current source

Both approval continuation APIs now require explicit non-`None` user input and a matching persisted approval cursor. Direct approval continuation validates task state, stored outcome, runtime ID, thread ID and resume token before transitioning to `RUNNING`.

This remains PREPARED rather than proven until the regression suite actually executes.

## Finding 4 — side-effect recovery is conservative

`RuntimeRecoveryService` checks the Nika `IdempotencyLedger` before automatic crash continuation. `PENDING` or `UNCERTAIN` external operations produce `RECONCILE_SIDE_EFFECTS`, preventing blind replay.

This is the correct product boundary: framework checkpoints do not replace provider reconciliation or stable idempotency keys for external actions.

## Required executable M2 gate extension

Before PR #3 may be called executable-green, add and run deterministic fault-injection tests for the product persistence boundary in addition to the existing LangGraph/SQLite tests:

1. atomic durable start: no observable state may contain `RUNNING` durable work without its Nika runtime-session cursor;
2. duplicate start with an existing persisted cursor fails closed and does not overwrite that cursor;
3. injected failure during durable-start transaction rolls back both task and session state;
4. injected process-loss states around `_finish()` classify conservatively on next startup;
5. approval and idempotency recovery tests remain green;
6. exact PR SHA passes Ruff, compile and full `.[dev,agent]` pytest.

## Integration consequence

M2 remains IMPLEMENTED/PREPARED but not INTEGRATED. This review adds a real pre-integration defect to the M2 gate. Do not begin unchecked M3+ production implementation until the runner is restored and this boundary is fixed and executable-green.
