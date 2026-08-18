# M2 restart recovery and side-effect safety

Updated: 2026-08-18.

## Problem closed by this batch
LangGraph persists graph checkpoints by `thread_id`, but Nika must not require UI/workspace code to remember framework identifiers across process restarts. Nika also needs a framework-neutral rule for external side effects that might be replayed after an interrupt or crash.

## Upstream evidence
Current LangGraph documentation defines `thread_id` as the persistent pointer used to load and resume checkpointed state. Interrupt documentation states that the same thread must be reused on resume and warns that code before an interrupt can run again. Durable execution guidance recommends idempotent side effects and idempotency keys because a failed step may be re-executed.

## Nika adaptation
Nika schema v3 adds `runtime_sessions`:
- keyed by Nika `task_id`;
- stores `runtime_id`, framework `thread_id`, opaque `resume_token`, last resumable outcome and timestamp;
- `(runtime_id, thread_id)` is unique;
- terminal/non-resumable results remove the active session pointer.

`TaskRuntimeCoordinator.resume_saved()` recreates a resume request from only the Nika task ID. It validates runtime ownership before changing task state. WAITING_APPROVAL resumes as approval; PAUSED and resumable FAILED states return through READY -> RUNNING before framework continuation. A completed task removes its active runtime session record.

This registry is deliberately separate from the LangGraph checkpointer: LangGraph owns framework checkpoint bytes; Nika owns product identity, state, audit and the pointer needed by the UI after restart.

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

## Prepared proofs
`tests/test_runtime_persistence.py` covers:
- approval session survives full coordinator recreation and resumes using only `task_id`;
- PAUSED session resumes after recreation;
- wrong runtime cannot consume another runtime's persisted session;
- duplicate same idempotency key/input is deduplicated;
- same key with different input is rejected;
- UNCERTAIN cannot be completed without explicit reconciliation;
- explicit reconciliation can close an externally verified uncertain operation.

All tests remain PREPARED, not PASSED, until GitHub Actions actually receives a runner and executes them.
