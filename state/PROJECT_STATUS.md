# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains 6.0%.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 runtime selection/adapter/durability package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection`, not INTEGRATED, and receives no final percentage credit until real framework tests execute and are green.

## Current milestone
M1 integration is still blocked by GitHub Actions account billing/spending infrastructure. Parallel safe M2 preparation continues on the dependent branch without bypassing the M1 merge gate.

## M1 candidate
PR #2: typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Current PR head: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.

## Current infrastructure blocker
The latest known PR #2 Actions job fails before workflow steps execute. Prior exact annotation identifies account payment failure or Actions spending-limit configuration. No runner/steps means this is infrastructure evidence, not code-test evidence. Do not merge PR #2 or credit M1 until Ruff/compile/pytest actually execute successfully.

## M2 large coherent batch — current cycle
Dependent branch: `dev/m2-runtime-selection`.
Head before this status commit: `c9f36e3f6391caf67b033cc91f75cf927ab776a4`.

### Reuse/architecture evidence
- ADAPT LangGraph behind framework-neutral `AgentRuntimePort`.
- REUSE `langgraph-checkpoint-sqlite` for the first local durable checkpoint proof.
- REUSE `aiosqlite` because Nika invokes LangGraph asynchronously and upstream requires the async SQLite saver for async graph execution.
- KEEP Microsoft Agent Framework as a secondary adapter/migration candidate.
- Nika task IDs, task state, audit, approvals, permissions and product contracts remain Nika-owned.

### Defect found and repaired before integration
The earlier M2 branch paired `graph.ainvoke()` with synchronous `SqliteSaver`. Current official LangGraph source states the synchronous saver deliberately raises `NotImplementedError` for its async checkpoint methods and directs async callers to `AsyncSqliteSaver`.

Repair implemented entirely inside the adapter boundary:
- `open_langgraph_sqlite()` is now an async context manager;
- uses `aiosqlite` + `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`;
- awaits saver setup;
- owns and closes the async connection;
- closes the connection on setup failure;
- forces `LANGGRAPH_STRICT_MSGPACK=true` before saver creation;
- retains idempotent handle close behavior;
- `aiosqlite` added to `agent` dependencies;
- M2 CI definition installs `.[dev,agent]` so real LangGraph tests will run once GitHub runner allocation works.

### Real-framework proof tests now prepared
New `tests/test_langgraph_real_durability.py` uses actual LangGraph graph APIs and actual SQLite checkpoint persistence, not fake graph objects.

Prepared acceptance scenarios:
1. completed preparation node performs an external side effect, later node fails, all graph/runtime/checkpointer objects are destroyed, fresh objects reopen the DB and resume without repeating the completed side effect;
2. approval interrupt is persisted, all runtime/checkpointer objects are destroyed, fresh objects resume the same thread with approval, and the completed preparation side effect remains exactly once;
3. a persisted checkpoint blob is deliberately corrupted; resume must return typed FAILED and must not silently restart the thread or repeat the prior side effect.

Existing deterministic tests still cover max-step bounds, result/interrupt normalization, approval and ordinary resume, exception normalization, framework-output isolation, capability truthfulness, runtime registry, TaskQueue/AuditLog coordinator mapping and strict checkpoint configuration.

### Security evidence
Current upstream LangGraph checkpoint documentation warns that unrestricted checkpoint deserialization may execute code if checkpoint storage is compromised. Nika therefore forces strict MsgPack deserialization at the construction boundary and does not expose it as a user-disableable option.

## Test truth
- Source and proof code are committed on the development branch.
- GitHub Actions cannot currently provide executable evidence because the account-level runner allocation blocker remains.
- Therefore this cycle does NOT claim that Ruff/compile/pytest or the new real LangGraph durability scenarios passed.
- No M1 or M2 product weight is credited without executable evidence.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: primary runtime selected; adapter/coordinator/async checkpoint boundary and real durability proof suite IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. When runners execute: run/fix/merge PR #2 only if its M1 suite is actually green.
3. Run the M2 PR with `.[dev,agent]`; execute/fix the real LangGraph SQLite restart, approval recreation and corruption tests.
4. Rebase/retarget M2 onto green `main` only after M1 is integrated.
5. Extend the real proof through `TaskRuntimeCoordinator` with actual LangGraph persistence and define/prove cancellation semantics before advertising cancellation.
6. Only then award M1/M2 weighted progress according to closed acceptance gates.
