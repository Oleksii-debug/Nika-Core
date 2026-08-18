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
Includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. M2 extends the database migration chain from schema v2 to v3 without changing the M1 product contract.

## M2 current branch
PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
PR remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Cancellation is truthful bounded in-process cancellation; Nika does not claim resume from the middle of an interrupted node.
- Runtime requests support positive wall-clock deadlines, typed TIMEOUT/TRANSIENT/INVALID_RESUME/DUPLICATE_ACTIVE/INTERNAL failures and fail-closed explicit retry policy with bounded backoff.

## M2 persisted recovery + side-effect safety package — current cycle
Official LangGraph docs were re-checked before code. They confirm that `thread_id` is the durable pointer used to load/resume checkpoint state, the same thread must be reused on resume, and side effects around interrupts/re-execution must be idempotent or protected by stable idempotency semantics.

Implemented on PR #3:
- SQLite schema v3 with `runtime_sessions` and `idempotency_records` migrations.
- `RuntimeSessionStore` keeps Nika-owned task -> runtime/thread/resume-token mapping separate from LangGraph checkpoint bytes.
- `TaskRuntimeCoordinator` now records resumable WAITING_APPROVAL/PAUSED/FAILED results before exposing the resumable task state and removes active pointers after terminal completion/cancellation/non-resumable failure.
- `resume_saved()` reconstructs the framework-neutral resume request using only Nika `task_id`, validates runtime ownership, and supports process recreation for approval, paused and resumable failed tasks.
- wrong runtime ownership fails before task-state mutation.
- `IdempotencyLedger` reserves stable operation keys with input fingerprints, deduplicates exact repeats, rejects key reuse for different input, records COMPLETED/UNCERTAIN, and requires an explicit reconciliation path before an uncertain external side effect can be closed as completed.
- external provider-native idempotency remains preferred; Nika's ledger is the cross-provider fail-closed safety record, not a claim of universal exactly-once delivery.
- documentation: `docs/RUNTIME_RECOVERY_AND_SIDE_EFFECTS.md`; reuse classifications updated in `docs/THIRD_PARTY_ADOPTION.md`.

## Prepared tests in this batch
`tests/test_runtime_persistence.py` prepares proofs that:
1. approval state survives complete coordinator recreation and resumes using only `task_id`;
2. PAUSED state resumes after recreation;
3. a different runtime cannot consume another runtime's stored session;
4. exact same idempotency key/input deduplicates;
5. same key with changed input is rejected;
6. UNCERTAIN external side effect cannot pass through normal completion;
7. an explicitly externally reconciled uncertain result can be closed.

Existing migration proof now expects schema v3 and verifies both new tables are created when upgrading an older database.

## Current infrastructure blocker
Re-checked Actions at the beginning of this cycle. PR #2 run `32073570804` is still a completed failure with job `95521808540` returning `steps = null`; no runner executed Ruff, compile or pytest. Previously captured GitHub annotation identified account payment failure or Actions spending-limit configuration. This remains infrastructure evidence, not code-test evidence.

## Test truth
- Source/tests/docs for this persisted recovery/idempotency package are committed.
- No test in this new package is claimed as PASSED because hosted CI still cannot allocate a runner.
- M1/M2 percentage credit remains zero until executable evidence exists.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: runtime selection, async durability, restart/approval/corruption proofs, cancellation, deadline/retry, Nika-owned persisted resume mapping and external side-effect idempotency safety IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
4. Execute real LangGraph/SQLite restart/no-repeat, approval recreation, corrupt-checkpoint fail-closed, cancellation, timeout/retry and persisted-session proofs together.
5. Add tool-adapter integration of the idempotency ledger only together with the first real side-effecting tool/MCP boundary; do not invent fake exactly-once guarantees in M2.
6. Only after executable M2 evidence is green, close M2 and move into M3 memory/scheduler/resource control.
