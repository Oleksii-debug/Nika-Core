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
M1 integration is still externally blocked by GitHub Actions account billing/spending runner allocation. Safe M2 work continues on the dependent branch without bypassing the M1 merge gate.

## M1 candidate
PR #2 head: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.
Includes typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap.

## M2 current branch
PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
Latest head after the retry/deadline package is recorded by PR metadata/status comments. PR remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Cancellation is truthful bounded in-process cancellation; Nika does not claim resume from the middle of an interrupted node.

## M2 retry/deadline package — current cycle
A new framework-neutral bounded execution slice is implemented:
- `RuntimeRequest` and `RuntimeResumeRequest` accept optional positive `timeout_seconds` in addition to `max_steps`.
- `RuntimeErrorCode` distinguishes TIMEOUT, TRANSIENT, INVALID_RESUME, DUPLICATE_ACTIVE and INTERNAL failures.
- LangGraph invocation deadlines use `asyncio.wait_for()` on the exact active task and return typed FAILED/TIMEOUT, not fabricated user cancellation.
- timeout/internal LangGraph failures retain the stable thread ID as a resume token, but timeout is not automatically retried.
- Nika-owned `RetryPolicy` is fail-closed: retries disabled by default; exact retryable error codes must be opted in; durable resume token is required unless an explicit caller allows fresh replay; exponential backoff is bounded.
- `TaskRuntimeCoordinator` maps a safe retry through RUNNING -> RETRYING -> RUNNING, audits `runtime.retry_scheduled` and `runtime.retry_started`, and resumes through the exact returned token instead of blindly replaying original input.
- untyped/internal failures are not automatically retried.
- documentation: `docs/RUNTIME_RETRY_TIMEOUT.md`; reuse record updated in `docs/THIRD_PARTY_ADOPTION.md`.

## Prepared tests for this cycle
`tests/test_runtime_retry_timeout.py` prepares deterministic proofs that:
1. non-positive deadlines fail before execution;
2. a slow graph becomes typed TIMEOUT rather than CANCELLED;
3. an explicitly TRANSIENT durable failure retries by resume token and completes;
4. a transient failure without a resume token is not replayed by default;
5. exponential retry backoff is bounded.

## Current infrastructure blocker
Latest Actions runs for both M1 and current M2 still fail before runner execution. Jobs return `steps = null`; no Ruff/compile/pytest step is executed. Previously captured GitHub annotation identified account payment failure or Actions spending-limit configuration. This is infrastructure evidence, not code-test evidence.

## Test truth
- Source/tests/docs for the retry/deadline package are committed.
- GitHub Actions on the current M2 head again failed with `steps = null` before any test command ran.
- Therefore no newly prepared test is claimed as passed, and M1/M2 percentage credit remains zero.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: runtime selection, async durability, restart/approval/corruption proofs, cancellation, typed timeout and safe explicit retry policy IMPLEMENTED/PREPARED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Re-check Actions infrastructure first.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix any real API/runtime failures.
4. Execute all real restart/no-repeat, approval recreation, corruption fail-closed, cancellation and retry/deadline proofs.
5. Only after executable M2 evidence is green, close M2 and move into M3 memory/scheduler/resource control.
6. Award M1/M2 weighted progress only from closed acceptance-gate evidence.
