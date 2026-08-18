# M2 restart recovery and side-effect safety

Updated: 2026-08-18.

## Problem closed by this package
LangGraph persists graph checkpoints by `thread_id`, but Nika must not require UI/workspace code to remember framework identifiers across process restarts. Nika also needs a framework-neutral rule for external side effects that might be replayed after an interrupt or crash.

A second, more subtle crash window is now explicitly closed: the whole Nika process can disappear after LangGraph has already written one or more checkpoints but **before** `runtime.run()` returns a `RuntimeResult`. If Nika waits until the result to store its task -> runtime/thread mapping, LangGraph still has durable state but the next Nika process no longer knows which thread belongs to the product task.

## Upstream evidence
Current LangGraph documentation defines `thread_id` as the persistent pointer used to load and resume checkpointed state. It also documents checkpointing at graph steps and pending writes for successful work in a partially failed super-step. Interrupt documentation states that the same thread must be reused on resume. Durable execution guidance requires side effects that might be replayed to be idempotent or protected by stable idempotency semantics.

For asynchronous graph execution Nika continues to use the official `AsyncSqliteSaver` path. SQLite is intentionally a local desktop durability target, not a claim that it is the correct high-concurrency server database.

## Nika runtime-session adaptation
Nika schema v3 contains `runtime_sessions`:
- keyed by Nika `task_id`;
- stores `runtime_id`, framework `thread_id`, opaque `resume_token`, state marker/outcome and timestamp;
- `(runtime_id, thread_id)` is unique;
- terminal/non-resumable results remove the runtime session pointer.

`RuntimeSessionStore.record_active()` now writes an ACTIVE marker **before** a durable runtime invocation is awaited when the runtime exposes a stable initial recovery token. LangGraph exposes `initial_resume_token()` and returns its `thread_id`, because that is the official durable checkpoint cursor.

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

This closes the prior gap where only WAITING_APPROVAL/PAUSED/FAILED results were recoverable after coordinator recreation, while an operating-system/process crash during an in-flight run could lose Nika's routing pointer.

## External side-effect ledger
Schema v3 also adds `idempotency_records`. `IdempotencyLedger` is CUSTOM Nika safety infrastructure because external APIs do not share one universal idempotency implementation.

Lifecycle:
1. reserve a stable `operation_key` with task ID, operation type and input fingerprint before an external side effect;
2. repeated reservation of exactly the same operation returns the same record;
3. reusing the key for different input fails closed;
4. after known success mark COMPLETED with safe result metadata;
5. if the process loses certainty about whether the external action happened, mark UNCERTAIN;
6. UNCERTAIN cannot be silently marked complete by the normal path; an explicit external reconciliation call is required.

A PENDING/UNCERTAIN record is not permission to blindly replay an email, publication, deletion or financial/high-impact action. Tool adapters must reconcile with the remote system or use that provider's native idempotency key where available.

## Safety boundary
This does not make arbitrary third-party tools exactly-once. It gives Nika a durable fail-closed primitive and stable operation identity. Exactly-once behavior still depends on provider semantics, reconciliation and correct placement of side effects.

Likewise, crash recovery resumes only from state the selected runtime actually persisted. Nika does not claim recovery from the middle of an arbitrary synchronous side effect or from bytes that were never durably committed.

## Prepared proofs
`tests/test_runtime_persistence.py` covers:
- approval session survives full coordinator recreation and resumes using only `task_id`;
- PAUSED session resumes after recreation;
- wrong runtime cannot consume another runtime's persisted session;
- duplicate same idempotency key/input is deduplicated;
- same key with different input is rejected;
- UNCERTAIN cannot be completed without explicit reconciliation;
- explicit reconciliation can close an externally verified uncertain operation.

`tests/test_runtime_crash_window.py` adds the missing pre-result crash proofs:
- a simulated abrupt process loss bypasses normal `Exception` handling after the task becomes RUNNING;
- the prebound ACTIVE session survives and contains the runtime/thread recovery cursor;
- a new coordinator resumes using only the Nika task ID and reaches COMPLETED;
- a normal terminal result removes the prebound active pointer;
- a stale active pointer cannot reopen a terminal task.

All new tests remain PREPARED, not PASSED, until executable CI actually receives a runner and runs them.
