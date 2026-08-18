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
M1 integration remains externally blocked by GitHub Actions account billing/spending runner allocation. Safe M2 review/testability work continues, but no unchecked M3+ functional backlog is allowed while M1/M2 executable evidence is unavailable.

## Exact branches
- Last proven green `main`: `df48f70b738f9227cad1df08ce3d7f40115b5f08`.
- M1 PR #2 current head: `58f5d49c10389216e0f26c28747a820faf9325c3`.
- M2 PR #3 branch: `dev/m2-runtime-selection`; the previous source head was `b7b4dc8daee2f9050d2d8f1f3e68508db6bfb786`. This cycle added crash-consistency review/status commits; refresh PR metadata for the final exact head before any merge/test claim.
- PR #3 still targets an older M1 base commit and must be rebased/retargeted only after M1 is executable-CI green and integrated.
- Governance PR #4 (`dev/parallel-development-policy`) is open and mergeable, but remains separate from M1/M2 integration evidence.

## M1 candidate
PR #2 includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. The latest M1 batch also normalizes key modifier order/aliases and unifies verification through `scripts/verify.py`.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Runtime requests support positive wall-clock deadlines, typed failures and fail-closed explicit retry policy with bounded backoff.
- `RuntimeSessionStore` durably maps Nika task -> runtime/thread/resume token and prebinds an ACTIVE pointer before durable execution is expected to become recoverable.
- `IdempotencyLedger` provides framework-neutral stable operation keys, input fingerprints and fail-closed reconciliation for external side effects.
- `RuntimeRecoveryService` inventories persisted sessions after process recreation and separates safe crash continuation from approval/manual/reconciliation/error cases.
- Approval continuation APIs require explicit decisions and persisted cursor ownership before transitioning work back to RUNNING.

## Current cycle — crash-consistency source review
A focused manual review of the M2 persistence boundaries found a real pre-integration defect in the current `TaskRuntimeCoordinator.start()` ordering.

Current durable-start order is task `READY -> RUNNING`, audit append, then runtime-session cursor binding. If the whole process disappears after the task transition but before the session row is committed, Nika can be left with stale `RUNNING` durable work that has no Nika-owned runtime/thread cursor for startup recovery. `RuntimeRecoveryService` inventories runtime sessions, so this state is not safely recoverable from task ID alone.

The defect and required remediation are now canonical in `docs/M2_CRASH_CONSISTENCY_REVIEW.md`.

Required M2 fix before integration:
- make durable start crash-consistent, preferably by persisting the task transition and initial durable session pointer in one SQLite transaction rather than merely reversing two independent writes;
- reject a fresh start when a persisted runtime cursor already exists instead of overwriting recovery state;
- add deterministic fault-injection tests for transaction rollback and process-loss windows;
- prove `_finish()` crash windows classify fail-closed and never reopen terminal work;
- keep existing approval/idempotency recovery tests green.

This review reduces integration risk but is not executable evidence and does not increase product progress.

## Reuse-first digital worker architecture already recorded
- ADAPT Microsoft UFO² as first Windows computer-use proof candidate rather than rebuilding a full Windows AgentOS.
- REUSE Playwright as deterministic browser automation baseline with semantic/accessibility locators.
- ADAPT Browser Use only as optional higher-level browser-agent layer if it beats the Playwright baseline measurably.
- ADAPT OpenHands Software Agent SDK/agent-server as first Software Factory coding-worker proof candidate.
- ADAPT Unified Planning for deterministic formal planning where a domain can be modeled explicitly.
- REUSE ONNX Runtime for compact specialist inference, never as a fake general reasoning brain.
- Heavy coding/browser/vision/model workers remain optional adapters/components rather than mandatory Nika Core dependencies.

## Infrastructure blocker
The latest canonical M1 workflow run remains GitHub Actions run `32108101409` (`Core CI`, run 69), conclusion `failure`. Its only job (`core`, job `95621584797`) has `steps = null`, confirming that checkout/install/Ruff/compile/pytest did not execute. This remains infrastructure evidence, not code-test evidence.

This cycle inspected that evidence but did not request another duplicate rerun.

## Test truth
- M1/M2 executable tests remain unproven in hosted CI.
- New crash-consistency review defines additional required fault-injection tests; they are not yet implemented or executed.
- No product percentage is credited for prepared/unexecuted tests, documentation or source review.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: IMPLEMENTED/PREPARED across durable runtime, recovery, explicit approval boundary, cancellation, timeout/retry and side-effect safety; a crash-consistency start-order defect is OPEN; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- Digital worker reuse architecture: RESEARCHED/DOCUMENTED, not IMPLEMENTED.
- Parallel-development governance: PREPARED on PR #4, not yet integrated into `main`.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates. Heavy coding/browser/vision/model workers should remain separable optional components instead of inflating mandatory Nika Core.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Respect the duplicate infrastructure-probe interval; do not burn repeated Actions runs on the same account-level failure.
2. Before M2 integration, implement the durable-start atomic persistence boundary plus fault-injection tests from `docs/M2_CRASH_CONSISTENCY_REVIEW.md` on the M2 branch.
3. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
4. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
5. Execute the complete LangGraph/SQLite durability suite plus new crash-consistency fault-injection cases; integrate M2 only if green.
6. Only after M2 is executable-green begin M3 production implementation. Independent future lanes may continue research/contracts only where they do not create unchecked functional backlog.
7. At the later Computer Interaction/Software Factory implementation gates, run bounded Playwright/UFO²/OpenHands proof branches before accepting them as dependencies.
