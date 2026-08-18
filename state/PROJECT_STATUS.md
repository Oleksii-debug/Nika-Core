# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 runtime selection/adapter/durability/cancellation package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3, not INTEGRATED, and receives no final percentage credit until real framework tests execute and are green.

## Current milestone
M1 integration is still blocked by GitHub Actions account billing/spending infrastructure. Safe M2 preparation continues on the dependent branch without bypassing the M1 merge gate.

## M1 candidate
PR #2: typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Current PR head remains `9f73aa4b4a560bd66410295ccc75303e1a037e70`.

## Current infrastructure blocker
The latest re-check of PR #2 Actions still returns a failed job with `steps = null`: no runner-executed Ruff/compile/pytest steps occurred. The previously confirmed GitHub account annotation identified recent account payment failure or Actions spending-limit configuration. This remains infrastructure evidence, not code-test evidence. Do not merge or credit M1/M2 until an actual runner executes the suites successfully.

## M2 large coherent batch — current cycle
Dependent PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
The branch now includes the previous async SQLite durability package plus a bounded cancellation/duplicate-execution package. PR #3 remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

### Reuse/architecture evidence
- ADAPT LangGraph behind framework-neutral `AgentRuntimePort`.
- REUSE `langgraph-checkpoint-sqlite` with `AsyncSqliteSaver` for local durable checkpoint proof.
- REUSE `aiosqlite` because Nika invokes LangGraph asynchronously and upstream requires the async SQLite saver for async graph execution.
- KEEP Microsoft Agent Framework as a secondary adapter/migration candidate.
- Nika task IDs, task state, audit, approvals, permissions and product contracts remain Nika-owned.

### Async persistence repair retained
The earlier M2 branch paired `graph.ainvoke()` with synchronous `SqliteSaver`. Current official LangGraph source states the synchronous saver does not support its asynchronous checkpoint methods and directs async callers to `AsyncSqliteSaver`.

Repair remains isolated to the adapter boundary:
- `open_langgraph_sqlite()` is an async context manager;
- uses `aiosqlite` + `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`;
- awaits saver setup;
- owns and closes the async connection;
- closes the connection on setup failure;
- forces `LANGGRAPH_STRICT_MSGPACK=true` before saver creation;
- `aiosqlite` is an `agent` dependency;
- M2 CI installs `.[dev,agent]` when runners are available.

### Durable restart/approval/corruption proofs prepared
`tests/test_langgraph_real_durability.py` uses actual LangGraph graph APIs and actual SQLite checkpoint persistence, not fake graph objects.

Prepared acceptance scenarios:
1. completed preparation node performs an external side effect, later node fails, all graph/runtime/checkpointer objects are destroyed, fresh objects reopen the DB and resume without repeating the completed side effect;
2. approval interrupt is persisted, all runtime/checkpointer objects are destroyed, fresh objects resume the same thread with approval, and the completed preparation side effect remains exactly once;
3. a persisted checkpoint blob is deliberately corrupted; resume must return typed FAILED and must not silently restart the thread or repeat the prior side effect;
4. a real async LangGraph node is held in-flight and Nika cancels the exact active invocation; the public result must be typed CANCELLED.

### Cancellation package prepared this cycle
`LangGraphRuntime` now tracks active invocations by `(task_id, thread_id)` using exact `asyncio.Task` objects.
- `RuntimeCapability.CANCELLATION` is advertised only for bounded in-process cancellation of an active invocation.
- `cancel()` cancels and awaits the exact active task, then cleans the active registry.
- a second cancel after completion returns `False` instead of fabricating success;
- concurrent duplicate execution for the same task/thread is rejected before a second graph call starts;
- both initial runs and resume invocations use the same protected execution path;
- `TaskRuntimeCoordinator.cancel()` records a cancellation request and lets the already-running coordinator path perform the final CANCELLED state transition, preventing double transitions;
- non-active cancellation attempts are audit-recorded without changing task state.

Deterministic adapter/coordinator tests and a real LangGraph cancellation scenario are committed but not claimed as passed until executable CI runs them.

### Cancellation durability boundary
Current LangGraph cooperative timeout/cancellation relies on asyncio cancellation. Upstream discussion also documents that process teardown immediately after cancellation can race with pending checkpoint/background writes. Therefore Nika does **not** claim resume-from-the-middle-of-a-cancelled-node. Durable restart guarantees remain tied to successfully persisted checkpoint boundaries. This is documented in `docs/RUNTIME_SELECTION.md`.

### Real Nika coordinator proof prepared
`tests/test_langgraph_real_coordinator.py` combines actual LangGraph persistence with actual Nika `SQLiteStore`, `TaskQueue`, `AuditLog` and `TaskRuntimeCoordinator`:
- task READY -> RUNNING -> WAITING_APPROVAL;
- destroy/recreate LangGraph runtime/checkpointer and Nika coordinator objects;
- approve/resume same persisted thread;
- final Nika task state COMPLETED;
- exact lifecycle and approval events persist in AuditLog.

### Resume identity safety
For the current LangGraph adapter, the stable resume token is the persisted LangGraph `thread_id`. The adapter fails closed before graph execution if `resume_token != thread_id`, preventing an inconsistent resume request from silently targeting a different persisted thread.

Existing tests also cover max-step bounds, result/interrupt normalization, approval and ordinary resume, exception normalization, framework-output isolation, capability truthfulness, runtime registry, TaskQueue/AuditLog mapping and strict checkpoint configuration.

## Test truth
- Source, real-framework proof code, cancellation proof code and integration proof code are committed on PR #3.
- GitHub Actions still cannot provide executable evidence because the account-level runner allocation blocker remains.
- Therefore this cycle does **not** claim that Ruff/compile/pytest or any newly prepared real LangGraph scenario passed.
- No M1 or M2 product weight is credited without executable evidence.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: primary runtime selected; adapter/coordinator/async checkpoint boundary, real durability suite, cancellation semantics/tests and real coordinator suite IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. When runners execute: run/fix/merge PR #2 only if its M1 suite is actually green.
3. Retarget/rebase PR #3 onto green `main` after M1 integration; execute `.[dev,agent]` Ruff/compile/pytest and fix any actual LangGraph/API failures.
4. Execute all real restart/no-repeat, approval recreate/resume, corruption fail-closed, real coordinator and real cancellation scenarios.
5. After executable M2 evidence is green, close M2 gates and only then move into M3 memory/scheduler/resource-control work.
6. Award M1/M2 weighted progress only from closed acceptance-gate evidence.
