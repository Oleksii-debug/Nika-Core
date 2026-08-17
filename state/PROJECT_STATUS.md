# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 runtime selection/adapter/durability package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3, not INTEGRATED, and receives no final percentage credit until real framework tests execute and are green.

## Current milestone
M1 integration is still blocked by GitHub Actions account billing/spending infrastructure. Parallel safe M2 preparation continues on the dependent branch without bypassing the M1 merge gate.

## M1 candidate
PR #2: typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Current PR head: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.

## Current infrastructure blocker
PR #2 and PR #3 Actions jobs currently fail before workflow steps execute. The prior exact GitHub annotation identified recent account payment failure or Actions spending-limit configuration. Latest PR #3 run `32081598223` also finished with a job whose `steps` are null. No runner-executed steps means this is infrastructure evidence, not Ruff/compile/pytest evidence. Do not merge or credit M1/M2 until an actual runner executes the suites successfully.

## M2 large coherent batch — current cycle
Dependent PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
Current exact head before this status commit: `862b260e96ffe67e719d19076743bffe4cbd8923`.
PR #3 is intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

### Reuse/architecture evidence
- ADAPT LangGraph behind framework-neutral `AgentRuntimePort`.
- REUSE `langgraph-checkpoint-sqlite` for the first local durable checkpoint proof.
- REUSE `aiosqlite` because Nika invokes LangGraph asynchronously and upstream requires the async SQLite saver for async graph execution.
- KEEP Microsoft Agent Framework as a secondary adapter/migration candidate.
- Nika task IDs, task state, audit, approvals, permissions and product contracts remain Nika-owned.

### Real defect found and repaired before integration
The earlier M2 branch paired `graph.ainvoke()` with synchronous `SqliteSaver`. Current official LangGraph source states the synchronous saver does not support its asynchronous checkpoint methods and directs async callers to `AsyncSqliteSaver`.

Repair is isolated to the adapter boundary:
- `open_langgraph_sqlite()` is an async context manager;
- uses `aiosqlite` + `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`;
- awaits saver setup;
- owns and closes the async connection;
- closes the connection on setup failure;
- forces `LANGGRAPH_STRICT_MSGPACK=true` before saver creation;
- retains idempotent handle close behavior;
- `aiosqlite` added to `agent` dependencies;
- M2 CI installs `.[dev,agent]` so real LangGraph tests can run when GitHub runner allocation works.

This confirms the ports/adapters design: a framework-specific defect was repaired without changing Nika domain contracts.

### Real-framework durability proofs prepared
`tests/test_langgraph_real_durability.py` uses actual LangGraph graph APIs and actual SQLite checkpoint persistence, not fake graph objects.

Prepared acceptance scenarios:
1. completed preparation node performs an external side effect, later node fails, all graph/runtime/checkpointer objects are destroyed, fresh objects reopen the DB and resume without repeating the completed side effect;
2. approval interrupt is persisted, all runtime/checkpointer objects are destroyed, fresh objects resume the same thread with approval, and the completed preparation side effect remains exactly once;
3. a persisted checkpoint blob is deliberately corrupted; resume must return typed FAILED and must not silently restart the thread or repeat the prior side effect.

### Real Nika coordinator proof prepared
`tests/test_langgraph_real_coordinator.py` now combines actual LangGraph persistence with actual Nika `SQLiteStore`, `TaskQueue`, `AuditLog` and `TaskRuntimeCoordinator`:
- task READY -> RUNNING -> WAITING_APPROVAL;
- destroy/recreate LangGraph runtime/checkpointer and Nika coordinator objects;
- approve/resume same persisted thread;
- final Nika task state COMPLETED;
- exact lifecycle and approval events persist in AuditLog.

### Resume identity safety
For the current LangGraph adapter, the stable resume token is the persisted LangGraph `thread_id`. The adapter now fails closed before graph execution if `resume_token != thread_id`, preventing an inconsistent resume request from silently targeting a different persisted thread. A deterministic test verifies no graph call occurs on mismatch.

Existing tests also cover max-step bounds, result/interrupt normalization, approval and ordinary resume, exception normalization, framework-output isolation, capability truthfulness, runtime registry, TaskQueue/AuditLog mapping and strict checkpoint configuration.

### Security evidence
Current upstream LangGraph checkpoint guidance warns about unsafe deserialization when checkpoint storage is compromised. Nika forces strict MsgPack deserialization at the construction boundary and does not expose it as a user-disableable option.

## Test truth
- Source, real-framework proof code and integration proof code are committed on PR #3.
- GitHub Actions still cannot provide executable evidence because the account-level runner allocation blocker remains.
- Therefore this cycle does **not** claim that Ruff/compile/pytest or any newly prepared real LangGraph scenario passed.
- No M1 or M2 product weight is credited without executable evidence.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: primary runtime selected; adapter/coordinator/async checkpoint boundary, real durability suite and real coordinator suite IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. When runners execute: run/fix/merge PR #2 only if its M1 suite is actually green.
3. Retarget/rebase PR #3 onto green `main` after M1 integration; execute `.[dev,agent]` Ruff/compile/pytest and fix any actual LangGraph/API failures.
4. Run the real restart/no-repeat, approval recreate/resume, corruption fail-closed and real coordinator persistence proofs.
5. Define and prove cancellation semantics before adding `RuntimeCapability.CANCELLATION`.
6. Only then award M1/M2 weighted progress according to closed acceptance gates.
