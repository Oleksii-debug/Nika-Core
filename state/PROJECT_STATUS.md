# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 durable runtime package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3 but not INTEGRATED; its 11% weight is not credited until real framework tests execute and are green.

## Current milestone
M1 integration remains externally blocked by GitHub Actions account billing/spending runner allocation. Safe dependent M2 work continues without bypassing the M1 merge gate.

## M1 candidate
PR #2 head: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.
Includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. M2 extends the database migration chain without changing the M1 product contract.

## M2 current branch
PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
Current branch head after the active crash-window package: `26775f1bf848299202946419e9cc696d3d816a3c` before this status commit.
PR remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Cancellation is truthful bounded in-process cancellation; Nika does not claim resume from the middle of an interrupted node.
- Runtime requests support positive wall-clock deadlines, typed TIMEOUT/TRANSIENT/INVALID_RESUME/DUPLICATE_ACTIVE/INTERNAL failures and fail-closed explicit retry policy with bounded backoff.
- `RuntimeSessionStore` durably maps Nika task -> runtime/thread/resume token for returned resumable outcomes.
- `IdempotencyLedger` provides framework-neutral stable operation keys, input fingerprints and UNCERTAIN reconciliation for external side-effect safety.

## M2 active process-loss recovery package — current cycle
Fresh official LangGraph documentation was checked before code. It confirms that checkpoints are organized by `thread_id`, `thread_id` is the primary cursor used to load/resume persisted state, checkpoint/pending writes provide fault tolerance at graph step boundaries, and asynchronous execution uses asynchronous checkpointer methods.

A real product gap was identified: before this cycle Nika only persisted its task -> runtime/thread mapping after `runtime.run()` returned a resumable result. If the entire process disappeared after LangGraph had already persisted checkpoints but before a RuntimeResult returned, LangGraph could still have durable work while the next Nika process no longer knew which thread belonged to the Nika task.

Implemented on PR #3:
- `RuntimeSessionStore.record_active()` writes a minimal ACTIVE runtime binding before a durable invocation is awaited.
- ACTIVE is represented as a Nika-owned session marker without duplicating or parsing LangGraph checkpoint bytes.
- `LangGraphRuntime.initial_resume_token()` exposes `thread_id` as the pre-run durable cursor because upstream checkpoint lookup is keyed by that thread ID.
- `TaskRuntimeCoordinator.start()` prebinds the active session before entering the runtime.
- abrupt process loss that bypasses normal exception/result handling therefore leaves the Nika routing pointer durable.
- `resume_saved()` recognizes ACTIVE records after process recreation, validates runtime ownership, explicitly repairs stale RUNNING state through PAUSED -> READY -> RUNNING, audits crash recovery and continues from the same durable cursor.
- ACTIVE records cannot reopen terminal/incompatible task states; that mismatch fails closed.
- normal terminal completion/cancellation/non-resumable failure removes the prebound active pointer.
- documentation expanded in `docs/RUNTIME_RECOVERY_AND_SIDE_EFFECTS.md`.

## Prepared tests in this cycle
New `tests/test_runtime_crash_window.py` prepares proofs that:
1. simulated abrupt process loss after RUNNING leaves the prebound durable task/runtime/thread pointer;
2. complete coordinator recreation can resume the task using only Nika `task_id`;
3. crash recovery reaches COMPLETED and clears the active session;
4. normal terminal completion also clears the prebound active pointer;
5. a stale active pointer cannot reopen a terminal task and fails closed.

Existing persistence/idempotency/restart/cancellation/deadline/retry tests remain in the M2 package.

## Current infrastructure blocker
At the beginning of this cycle the failed M1 workflow run `32073570804` was explicitly re-run because M1 integration is a meaningful gate. GitHub accepted the rerun request, but the new job `95587744023` again completed failure with `steps = null`. No runner executed checkout, install, Ruff, compile or pytest. Previously captured GitHub annotation identified account payment failure or Actions spending-limit configuration. This remains infrastructure evidence, not code-test evidence.

## Test truth
- Source/tests/docs for the active process-loss recovery package are committed.
- No new test is claimed as PASSED because hosted CI still cannot allocate a runner.
- M1/M2 percentage credit remains zero until executable evidence exists.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: runtime selection, async durability, restart/approval/corruption proofs, cancellation, deadline/retry, persisted resume mapping, external side-effect idempotency safety and pre-result active crash recovery IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
4. Execute the full real LangGraph/SQLite durability suite together, including pre-result process-loss recovery, completed-step no-repeat, approval recreation, corrupt-checkpoint fail-closed, cancellation, timeout/retry and persisted-session proofs.
5. After M2 is executable-green, move into M3 memory/scheduler/resource control as one large coherent package; do not credit M3 early.
