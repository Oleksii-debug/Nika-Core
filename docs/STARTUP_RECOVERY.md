# Startup recovery — Nika Core

Date: 2026-08-18
Status: M2 IMPLEMENTED/PREPARED, not integration-proven while GitHub Actions runners remain unavailable.

## Reuse decision

Official LangGraph persistence documentation was re-checked before this package. LangGraph already provides the correct framework-level primitives: checkpoints are organized by `thread_id`, the thread ID is the durable cursor used to load/resume state, pending writes avoid re-running successful graph work, and interrupts require the same durable thread when resuming.

Decision:
- REUSE — LangGraph checkpoint/thread persistence for graph state and fault-tolerant step recovery.
- ADAPT — Nika keeps only an opaque task -> runtime/thread pointer in `RuntimeSessionStore` and never parses or duplicates LangGraph checkpoint bytes.
- CUSTOM (thin) — Nika `RuntimeRecoveryService`, because an orchestration framework cannot know Nika task states, registered runtimes, permission/approval policy or external side-effect ledger. Product-level startup recovery must combine those Nika-owned facts before deciding what can resume.

Upstream references checked:
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://docs.langchain.com/oss/python/langgraph/interrupts

## Startup recovery policy

On process recreation Nika inventories every persisted runtime session and classifies it before any replay:

1. `AUTO_RESUME_CRASH`
   - session is still marked ACTIVE;
   - Nika task is still RUNNING, indicating abrupt process loss;
   - required runtime is registered;
   - there is no PENDING or UNCERTAIN external side-effect record for the task.
   Only this class is eligible for bounded automatic continuation.

2. `WAITING_APPROVAL`
   - persisted runtime outcome is WAITING_APPROVAL;
   - Nika task state is WAITING_APPROVAL.
   Never auto-resume because a human approval/rejection/edit value is required.

3. `MANUAL_RESUME`
   - deliberately PAUSED work;
   - FAILED resumable work;
   - an ACTIVE pointer paired with PAUSED/FAILED Nika state.
   Require explicit operator intent instead of guessing.

4. `RECONCILE_SIDE_EFFECTS`
   - any external side-effect ledger entry for the task remains PENDING or UNCERTAIN.
   Automatic replay is forbidden until an adapter/provider determines whether the external action actually occurred. This is stricter than checking only UNCERTAIN: a hard process loss can leave a PENDING reservation before Nika has a chance to mark it uncertain.

5. `MISSING_RUNTIME`
   - the persisted runtime ID is not registered in the new process.
   Fail closed; never silently substitute a different orchestration engine.

6. `INCONSISTENT_STATE`
   - persisted session and Nika task state disagree or the task disappeared.
   Fail closed for diagnosis instead of manufacturing a recovery transition.

## Automatic recovery boundary

`RuntimeRecoveryService.resume_safe_crash_sessions()` is intentionally narrow:
- bounded by `max_count`;
- resumes only `AUTO_RESUME_CRASH` candidates;
- uses the original persisted runtime ID/thread through `TaskRuntimeCoordinator.resume_saved()`;
- never auto-approves human interrupts;
- never replays tasks with unresolved external side effects;
- records recovery inventory, attempts and failures in Nika Audit Log;
- a failure of one candidate does not cause unrelated candidates to be replayed blindly.

This service is a reusable application-level primitive. A future GUI/startup policy may choose whether to invoke automatic crash recovery immediately, show the inventory first, or require a user preference. The safety classification does not depend on the UI.

## External side-effect rule

`IdempotencyLedger.list_for_task()` now exposes the per-task operation inventory needed by startup recovery. Both `PENDING` and `UNCERTAIN` block automatic replay:
- `PENDING` can mean the process died in the dangerous window after reservation and before outcome recording;
- `UNCERTAIN` explicitly means the external result could not be proven.

Only provider reconciliation or a known completed record can remove this ambiguity. Nika never converts ambiguity into success and never blindly retries it.

## Prepared proof suite

`tests/test_runtime_startup_recovery.py` prepares deterministic proofs for:
- clean ACTIVE/RUNNING crash classification;
- PENDING side-effect replay blocking;
- approval waits never being auto-resumed;
- missing runtimes failing closed;
- bounded automatic recovery resuming only the safe crash candidate and leaving the blocked task untouched;
- invalid automatic recovery limits failing before a side effect.

These tests are PREPARED, not PASSED, until an executable runner actually runs them.
