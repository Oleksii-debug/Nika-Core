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
Current source head before this status commit: `13219c4a68a4e7366d4ca69497d9db0207466b45`.
PR remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Cancellation is truthful bounded in-process cancellation; Nika does not claim resume from the middle of an interrupted node.
- Runtime requests support positive wall-clock deadlines, typed TIMEOUT/TRANSIENT/INVALID_RESUME/DUPLICATE_ACTIVE/INTERNAL failures and fail-closed explicit retry policy with bounded backoff.
- `RuntimeSessionStore` durably maps Nika task -> runtime/thread/resume token and prebinds an ACTIVE pointer before durable execution so abrupt process loss does not orphan LangGraph checkpoints from the Nika task.
- `IdempotencyLedger` provides framework-neutral stable operation keys, input fingerprints and fail-closed reconciliation for external side effects.
- `RuntimeRecoveryService` now inventories all persisted sessions after process recreation and separates safe crash continuation from approval/manual/reconciliation/error cases.

## Current cycle — startup-wide recovery inventory and replay safety
Fresh official LangGraph persistence and interrupt documentation was checked before code. It confirms that checkpoint state is organized by `thread_id`, the thread ID is the durable cursor required for resume, pending writes prevent re-running already successful graph work, and human interrupts must resume on the same thread.

Reuse/adaptation decision:
- REUSE LangGraph checkpoint/thread persistence as graph execution truth.
- ADAPT via Nika's existing opaque runtime/thread pointer; do not duplicate or parse LangGraph checkpoint bytes.
- CUSTOM (thin) Nika startup recovery classification because the framework cannot know Nika task state, runtime registration, approval policy or external side-effect ledger.

Implemented on PR #3:
- added `RuntimeRecoveryService`, `RecoveryCandidate`, `RecoveryDisposition` and `RecoveryExecution`;
- startup inventory classifies every persisted runtime session deterministically;
- only an ACTIVE session paired with stale RUNNING state, a registered runtime and no unresolved external side effect is eligible for bounded automatic crash continuation;
- WAITING_APPROVAL is never auto-resumed because a human value is required;
- deliberate PAUSED/FAILED resumable work remains manual;
- missing runtime or inconsistent task/session state fails closed;
- PENDING and UNCERTAIN external side-effect records both block automatic replay. This closes a harder crash window where the process can disappear after reserving an external operation but before it can mark the outcome uncertain;
- automatic crash recovery is bounded by `max_count`, routes through `TaskRuntimeCoordinator.resume_saved()`, records audit events and isolates per-task recovery failures;
- `IdempotencyLedger.list_for_task()` exposes the stable per-task side-effect inventory needed by startup recovery;
- public runtime exports and adoption documentation were updated;
- detailed policy added in `docs/STARTUP_RECOVERY.md`.

## Prepared tests in this cycle
New `tests/test_runtime_startup_recovery.py` prepares proofs that:
1. a clean ACTIVE/RUNNING crash is classified as safe automatic recovery;
2. a PENDING external side effect blocks automatic continuation;
3. approval waits are never automatically approved/resumed;
4. missing runtimes fail closed instead of being substituted;
5. bounded startup recovery resumes only the safe crash session and leaves blocked work untouched;
6. invalid automatic recovery limits fail before recovery side effects.

Existing persistence/idempotency/crash-window/restart/cancellation/deadline/retry tests remain in the M2 package.

## Current infrastructure blocker
At the beginning of this cycle M1 workflow run `32073570804` was explicitly re-run again because M1 integration is the next meaningful gate. GitHub accepted the rerun request, but latest job `95598336284` completed failure with `steps = null`. No runner executed checkout, dependency installation, Ruff, compile or pytest. Previously captured GitHub annotation identified account payment failure or Actions spending-limit configuration. This remains infrastructure evidence, not code-test evidence.

A direct local clone/test fallback was attempted from the automation environment, but the local shell/Git client was not available through the current tool runtime, so no local execution is claimed as a substitute for CI.

## Test truth
- Source/tests/docs for startup-wide recovery are committed.
- No new test is claimed as PASSED because hosted CI still cannot allocate a runner and no equivalent local execution environment was available in this cycle.
- M1/M2 percentage credit remains zero until executable evidence exists.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: runtime selection, async durability, restart/approval/corruption proofs, cancellation, deadline/retry, persisted resume mapping, external side-effect idempotency safety, pre-result active crash recovery and startup-wide safe recovery inventory IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
4. Execute the full real LangGraph/SQLite durability suite together, including startup-wide recovery classification, pre-result process-loss recovery, completed-step no-repeat, approval recreation, corrupt-checkpoint fail-closed, cancellation, timeout/retry and persisted-session proofs.
5. After M2 is executable-green, move into M3 memory/scheduler/resource control as one large coherent package; do not credit M3 early.
