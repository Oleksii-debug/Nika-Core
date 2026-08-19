# M2 restart recovery and side-effect safety

Updated: 2026-08-19.

## Problem closed by this package
LangGraph persists graph checkpoints by `thread_id`, but Nika must not require UI/workspace code to remember framework identifiers across process restarts. Nika also needs a framework-neutral rule for external side effects that might be replayed after an interrupt or crash.

A second, more subtle crash window is explicitly closed: the whole Nika process can disappear after LangGraph has already written one or more checkpoints but **before** `runtime.run()` returns a `RuntimeResult`. If Nika waits until the result to store its task -> runtime/thread mapping, LangGraph still has durable state but the next Nika process no longer knows which thread belongs to the product task.

A third safety-critical crash window concerns cancellation. The user's Stop intent must win over automatic restart recovery. If Nika calls an external/runtime cancellation primitive before durably recording that intent, the process can disappear after the user asked to stop but before local task/session finalization. An old ACTIVE + RUNNING cursor could then look like an ordinary crash and be auto-resumed. The current cancellation protocol therefore persists the cancel operation **before** invoking the runtime and makes unresolved cancellation a recovery blocker.

## Reuse decision
**REUSE** Python `sqlite3` and the existing `SQLiteStore` transaction boundary. No new storage engine or dependency is introduced. Nika already owns the product-level task/session/idempotency semantics, so the cancellation protocol is **CUSTOM (thin)** policy implemented on top of the existing v3 `idempotency_records` table rather than a new generic transaction framework.

The standard SQLite transaction context remains the local atomicity primitive for Nika-owned state. Runtime/provider cancellation itself is external to that SQLite transaction and therefore follows the reserve -> act -> finalize/reconcile pattern instead of pretending that one database transaction can make an external side effect exactly-once.

## Upstream evidence
Current LangGraph documentation defines `thread_id` as the persistent pointer used to load and resume checkpointed state. It also documents checkpointing at graph steps and pending writes for successful work in a partially failed super-step. Interrupt documentation states that the same thread must be reused on resume. Durable execution guidance requires side effects that might be replayed to be idempotent or protected by stable idempotency semantics.

For asynchronous graph execution Nika continues to use the official `AsyncSqliteSaver` path. SQLite is intentionally a local desktop durability target, not a claim that it is the correct high-concurrency server database.

## Nika runtime-session adaptation
Nika schema v3 contains `runtime_sessions`:
- keyed by Nika `task_id`;
- stores `runtime_id`, framework `thread_id`, opaque `resume_token`, state marker/outcome and timestamp;
- `(runtime_id, thread_id)` is unique;
- terminal/non-resumable results remove the runtime session pointer.

`RuntimeSessionStore.record_active()` writes an ACTIVE marker **before** a durable runtime invocation is awaited when the runtime exposes a stable initial recovery token. LangGraph exposes `initial_resume_token()` and returns its `thread_id`, because that is the durable checkpoint cursor.

This pre-run record is deliberately tiny: it does not duplicate LangGraph checkpoint bytes. LangGraph owns graph state/checkpoints; Nika owns the product task identity, selected runtime and the routing cursor required to find that state again.

## Recovery after complete process loss
`TaskRuntimeCoordinator.resume_saved()` can recreate a resume request from only the Nika task ID.

For a previously returned resumable result:
- WAITING_APPROVAL resumes as approval;
- PAUSED and resumable FAILED states return through READY -> RUNNING before continuation.

For a pre-run ACTIVE record left behind by abrupt process loss:
1. validate runtime ownership before state mutation;
2. require the persisted Nika task to still be in a crash-compatible nonterminal state;
3. convert stale RUNNING to PAUSED, then READY -> RUNNING so recovery is explicit in the task event history;
4. audit `runtime.crash_recovery_started`;
5. resume the same durable runtime/thread using the prebound token;
6. remove the active pointer on terminal success/cancellation/non-resumable failure.

A stale ACTIVE pointer is **not** allowed to reopen a COMPLETED/CANCELLED/ARCHIVED task. That mismatch fails closed and leaves the terminal task unchanged for operator diagnosis.

This closes the gap where only WAITING_APPROVAL/PAUSED/FAILED results were recoverable after coordinator recreation, while an operating-system/process crash during an in-flight run could lose Nika's routing pointer.

## External side-effect ledger
Schema v3 contains `idempotency_records`. `IdempotencyLedger` is CUSTOM Nika safety infrastructure because external APIs do not share one universal idempotency implementation.

Lifecycle:
1. reserve a stable `operation_key` with task ID, operation type and input fingerprint before an external side effect;
2. repeated reservation of exactly the same operation returns the same record, while `reserve_once()` also tells a side-effect boundary whether this caller actually created the reservation;
3. reusing the key for different input fails closed;
4. after known success mark COMPLETED with safe result metadata;
5. if the process loses certainty about whether the external action happened, mark UNCERTAIN;
6. UNCERTAIN cannot be silently marked complete by the normal path; an explicit external reconciliation call is required;
7. a PENDING reservation may be released only when the external adapter explicitly reports that the action was not active/applied.

Caller-owned transaction variants keep task state, runtime-session pointers, audit evidence and ledger status in one local SQLite commit where they are one product invariant.

A PENDING/UNCERTAIN record is not permission to blindly replay an email, publication, deletion, cancellation or financial/high-impact action. Tool adapters must reconcile with the remote system or use that provider's native idempotency key where available.

## Crash-safe runtime cancellation
`TaskRuntimeCoordinator.cancel()` now uses a deterministic operation identity derived from runtime ID, Nika task ID and thread ID.

The protocol is:
1. in one local transaction reserve `runtime.cancel` as PENDING and append `runtime.cancel_requested`;
2. invoke the runtime cancellation primitive only if this call created that reservation;
3. if the runtime call raises a normal exception, mark the reservation UNCERTAIN and audit `runtime.cancel_uncertain`;
4. if the runtime proves there was no active operation (`False`), remove only the still-PENDING reservation and audit `runtime.cancel_not_active`, allowing a later explicit attempt;
5. if cancellation is accepted (`True`), atomically transition the Nika task to CANCELLED when still cancellable, remove its runtime-session cursor, complete the idempotency record and audit `runtime.cancel_accepted`;
6. a repeated call after COMPLETED returns the stored cancellation result without invoking the runtime again;
7. PENDING or UNCERTAIN cancellation is never blindly replayed.

Because startup recovery already treats any PENDING/UNCERTAIN side effect as `RECONCILE_SIDE_EFFECTS`, a process loss after durable cancel intent can no longer be classified as an ordinary auto-resumable crash.

The coordinator also defends the completion race. Once local task state is CANCELLED, a late result from the already-running coroutine may be recorded for diagnosis but cannot transition the task back to COMPLETED/FAILED or recreate a resumable session. The effective product result remains CANCELLED and `runtime.finished_after_cancel` records the late runtime observation.

## Safety boundary
This does not make arbitrary third-party tools exactly-once. It gives Nika a durable fail-closed primitive and stable operation identity. Exactly-once behavior still depends on provider semantics, reconciliation and correct placement of side effects.

Likewise, crash recovery resumes only from state the selected runtime actually persisted. Nika does not claim recovery from the middle of an arbitrary synchronous side effect or from bytes that were never durably committed.

A hard process loss while cancellation is PENDING deliberately requires reconciliation instead of guessing whether the runtime observed the cancellation request. This may require an operator/provider-specific check; it is safer than resurrecting work the user intended to stop or replaying a cancellation side effect blindly.

## Proof coverage
`tests/test_runtime_persistence.py` covers:
- approval session survives full coordinator recreation and resumes using only `task_id`;
- PAUSED session resumes after recreation;
- wrong runtime cannot consume another runtime's persisted session;
- duplicate same idempotency key/input is deduplicated;
- same key with different input is rejected;
- UNCERTAIN cannot be completed without explicit reconciliation;
- explicit reconciliation can close an externally verified uncertain operation.

`tests/test_runtime_crash_window.py` covers:
- a simulated abrupt process loss bypasses normal `Exception` handling after the task becomes RUNNING;
- the prebound ACTIVE session survives and contains the runtime/thread recovery cursor;
- a new coordinator resumes using only the Nika task ID and reaches COMPLETED;
- a normal terminal result removes the prebound active pointer;
- a stale active pointer cannot reopen a terminal task.

`tests/test_runtime_cancel_safety.py` covers:
- accepted cancellation commits CANCELLED state, session cleanup and completed dedup evidence;
- repeated accepted cancellation does not call the runtime twice;
- a proven not-active cancellation releases its PENDING reservation and permits a later explicit attempt;
- transport uncertainty becomes UNCERTAIN and blocks startup auto-resume;
- abrupt process loss after durable cancel intent leaves PENDING and also blocks auto-resume;
- a PENDING/UNCERTAIN cancellation cannot be replayed by a second cancel call;
- accepted cancellation wins a race against a later runtime completion;
- failure while committing local cancel finalization rolls back local state instead of exposing a partially terminal task.

These tests provide automated engineering evidence only. They do not imply HUMAN_TESTED or NVDA_VERIFIED.
