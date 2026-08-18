# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 durable runtime package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3 but not INTEGRATED; its 11% weight is not credited until real tests execute and are green.

## Current milestone
M1 integration remains externally blocked by GitHub Actions account billing/spending runner allocation. Safe M2 correctness/testability work continues; no unchecked M3+ functional backlog is allowed while M1/M2 executable evidence is unavailable.

## Exact branches
- Last proven green `main`: `df48f70b738f9227cad1df08ce3d7f40115b5f08`.
- M1 PR #2 head: `58f5d49c10389216e0f26c28747a820faf9325c3`.
- M2 PR #3 implementation/doc head before this status-only commit: `a20ef032b50facefbb7904190a4a60cef3b61f0d`.
- PR #3 still targets `dev/m1-foundation` and must be rebased/retargeted only after M1 is executable-CI green and integrated.
- Governance PR #4 remains separate PREPARED work and carries no integration credit.

## M1 candidate
PR #2 includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Latest M1 work normalizes modifier aliases/order and unifies verification through `scripts/verify.py`.

## M2 implemented/prepared capabilities
- LangGraph selected behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary migration/interop candidate.
- Async local durability uses `AsyncSqliteSaver` + `aiosqlite`; strict checkpoint deserialization is forced.
- Runtime/session persistence, restart recovery, approval boundaries, cancellation, deadline/retry safety and external side-effect idempotency/reconciliation are prepared.
- `RuntimeRecoveryService` separates safe crash continuation from approval/manual/reconciliation/error states and never blindly replays unresolved side effects.

## Current cycle — atomic durable-start crash-consistency fix
The previously open M2 start-order defect is now IMPLEMENTED/PREPARED in source.

Changes:
- `TaskQueue.transition_with_connection()` allows a validated state transition to participate in a caller-owned SQLite transaction.
- `RuntimeSessionStore.record_active_with_connection()` inserts a new ACTIVE recovery cursor in that same transaction.
- `TaskRuntimeCoordinator.start()` now commits durable task `READY -> RUNNING` plus the initial runtime-session cursor atomically when the runtime exposes an initial durable token.
- fresh start over an existing runtime cursor fails closed instead of overwriting crash-recovery ownership.
- non-durable runtimes retain the ordinary task transition path and are not represented as durable.

Fault-injection tests were added in `tests/test_runtime_crash_consistency.py` for:
1. failure after cursor insertion: task transition and cursor both roll back; runtime is never called;
2. duplicate existing cursor: fresh start fails and task remains READY while original cursor is preserved;
3. terminal result followed by session-delete failure: task remains terminal and startup recovery classifies the stale session as `INCONSISTENT_STATE`, never auto-resume.

This closes the source-level defect identified in `docs/M2_CRASH_CONSISTENCY_REVIEW.md`, but it remains PREPARED rather than proven until executable CI runs.

## Reuse-first digital-worker architecture already recorded
- ADAPT Microsoft UFO² as first Windows computer-use proof candidate.
- REUSE Playwright as deterministic semantic browser baseline.
- ADAPT Browser Use only if it measurably improves on that baseline.
- ADAPT OpenHands SDK/agent-server as first Software Factory coding-worker proof candidate.
- ADAPT Unified Planning for explicit deterministic planning domains.
- REUSE ONNX Runtime for compact specialist inference.

## Infrastructure blocker
Latest canonical M1 workflow remains GitHub Actions run `32108101409` (`Core CI`, run 69), conclusion `failure`; its job had `steps = null`, so checkout/install/Ruff/compile/pytest did not execute. This remains infrastructure evidence, not code-test evidence.

No duplicate runner probe was requested in this cycle under the configured probe-spacing rule.

## Test truth
- New crash-consistency tests are committed but NOT claimed PASSED.
- M1/M2 hosted executable tests remain unproven.
- No product percentage is credited for prepared/unexecuted tests or source review.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED/PREPARED including atomic durable start; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- Digital-worker reuse architecture: RESEARCHED/DOCUMENTED, not IMPLEMENTED.
- Parallel-development governance: PREPARED on PR #4, not integrated.

## Packaging policy
No EXE this cycle. Windows standalone is built only at milestone/user-test/release gates; heavy browser/coding/vision/model workers remain optional components.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Respect the duplicate GitHub Actions infrastructure-probe interval.
2. Perform another source-level consistency review around resumable `_finish()` and audit-failure boundaries without expanding into M3+ production code.
3. As soon as runners execute: run/fix/merge PR #2 only on genuine green M1 Ruff/compile/pytest evidence.
4. Rebase/retarget PR #3 onto green main and execute `.[dev,agent]` Ruff/compile/pytest.
5. Execute the complete LangGraph/SQLite durability suite plus new atomic-start/finalization fault-injection tests, approval, cancellation, timeout/retry, idempotency and startup recovery.
6. Integrate M2 only if the exact rebased SHA is green; only then begin M3 production implementation.