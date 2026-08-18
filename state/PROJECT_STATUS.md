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
M1 integration remains externally blocked by GitHub Actions account billing/spending runner allocation. Safe dependent M2 work may continue, but no unchecked M3+ functional backlog is allowed. While the runner blocker persists, cycles prioritize M1/M2 source review, testability, documentation and reuse research.

## Exact branches
- Last proven green `main`: `df48f70b738f9227cad1df08ce3d7f40115b5f08`.
- M1 PR #2 current head: `58f5d49c10389216e0f26c28747a820faf9325c3`.
- M2 PR #3 source head before this status commit: `aeafa5420511dace3f946f8717a3354e462980fd`.
- PR #3 still targets an older M1 base commit and must be rebased/retargeted only after M1 is executable-CI green and integrated.

## M1 candidate
PR #2 includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. The latest M1 batch also normalizes key modifier order/aliases and unifies verification through `scripts/verify.py`. M2 extends the database migration chain without changing the M1 product contract.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Runtime requests support positive wall-clock deadlines, typed failures and fail-closed explicit retry policy with bounded backoff.
- `RuntimeSessionStore` durably maps Nika task -> runtime/thread/resume token and prebinds an ACTIVE pointer before durable execution so abrupt process loss does not orphan checkpoints from the Nika task.
- `IdempotencyLedger` provides framework-neutral stable operation keys, input fingerprints and fail-closed reconciliation for external side effects.
- `RuntimeRecoveryService` inventories persisted sessions after process recreation and separates safe crash continuation from approval/manual/reconciliation/error cases.

## Current cycle — explicit persisted approval safety boundary
Source review found that generic task-ID resume could infer `APPROVAL` mode from a stored `WAITING_APPROVAL` outcome. Startup recovery did not call that path automatically, but the public coordinator API was too easy for a future GUI/plugin/workspace to misuse.

This cycle made the boundary fail closed:
- generic `TaskRuntimeCoordinator.resume_saved()` now rejects persisted approval waits without changing task/session state;
- new `resume_saved_approval()` is the explicit task-ID-based authorization path and requires a caller-supplied `approval_value` argument;
- explicit approval continuation verifies runtime ownership, persisted `WAITING_APPROVAL` outcome and Nika `WAITING_APPROVAL` task state before invoking the runtime;
- approval continuation has a separate `runtime.saved_approval_resumed` audit event and does not log the decision value;
- prepared persistence tests prove generic resume rejection, explicit approval success and rejection of the approval API for non-approval sessions;
- `docs/STARTUP_RECOVERY.md` now documents the API boundary so future UI/plugin work cannot treat generic “Continue” as authorization.

This is defense in depth: startup recovery already classified approval waits as human-only, and now the lower coordinator API enforces the same distinction.

## Reuse-first digital worker architecture already recorded
- ADAPT Microsoft UFO² as first Windows computer-use proof candidate rather than rebuilding a full Windows AgentOS.
- REUSE Playwright as deterministic browser automation baseline with semantic/accessibility locators.
- ADAPT Browser Use only as optional higher-level browser-agent layer if it beats the Playwright baseline measurably.
- ADAPT OpenHands Software Agent SDK/agent-server as first Software Factory coding-worker proof candidate.
- ADAPT Unified Planning for deterministic formal planning where a domain can be modeled explicitly.
- REUSE ONNX Runtime for compact specialist inference, never as a fake general reasoning brain.
- Heavy coding/browser/vision/model workers remain optional adapters/components rather than mandatory Nika Core dependencies.

## Infrastructure blocker
Most recent canonical M1 evidence remains a GitHub Actions job that failed before checkout/dependency/test steps (`steps = null`), with previously captured GitHub annotation identifying account payment failure or Actions spending-limit configuration. This is infrastructure evidence, not code-test evidence.

No explicit duplicate rerun was spent in this cycle. Branch updates did not produce executable workflow evidence. No new test is claimed PASSED.

## Test truth
- New approval-safety code, tests and documentation are committed on M2.
- M1/M2 executable tests remain unproven in hosted CI.
- No product percentage is credited for prepared/unexecuted tests or documentation.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: IMPLEMENTED/PREPARED across durable runtime, recovery, explicit approval boundary, cancellation, timeout/retry and side-effect safety; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- Digital worker reuse architecture: RESEARCHED/DOCUMENTED, not IMPLEMENTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates. Heavy coding/browser/vision/model workers should remain separable optional components instead of inflating mandatory Nika Core.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Respect the duplicate infrastructure-probe interval; re-check Actions only when the interval/configuration warrants it.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
4. Execute the full real LangGraph/SQLite durability suite together: startup recovery, pre-result process loss, no-repeat completed work, explicit approval recreation, corrupt checkpoint fail-closed, cancellation, timeout/retry and persisted sessions.
5. Only after M2 is executable-green begin M3 as one coherent implementation package.
6. When the roadmap reaches Computer Interaction/Software Factory implementation, run bounded proof branches for Playwright, UFO² and OpenHands before accepting them as dependencies.
