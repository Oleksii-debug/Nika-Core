# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- Overall proven final A–Z product progress is now **16.0%**.
- M2 durable runtime package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3 but not yet INTEGRATED; its 11% product weight is not credited until the exact reconciled M2 SHA passes executable CI and is merged.

## Current milestone
M2 integration is now the active gate. The previous GitHub Actions runner-allocation blocker is no longer current: hosted CI is executing normally again.

## Proven M1 evidence
- PR #2 exact green head: `67df93c355e813dfc297bd1111df40d3c4ad6175`.
- GitHub Actions `Core CI` run `32133041861` (run 74) completed `success` on that exact head.
- Its job executed checkout, Python setup, dependency installation and `python scripts/verify.py`; the verification step completed successfully.
- PR #2 was merged into `main` as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M1 therefore earns its full 10% acceptance-gate weight.

## M1 integrated capability
Typed/versioned configuration; ordered SQLite migrations; persisted Agent/Workspace registries; Audit Log; workspace discovery contract; central Action Registry; persisted remappable Keymap with conflict/clear/restore/import/export; normalized modifier aliases/order; shared `scripts/verify.py` harness.

## M2 implemented/prepared capabilities
- LangGraph selected behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains a secondary migration/interop candidate.
- Async local durability uses `AsyncSqliteSaver` + `aiosqlite`; strict checkpoint deserialization is forced.
- Runtime/session persistence, restart recovery, explicit approval boundaries, cancellation, timeout/retry safety and external side-effect idempotency/reconciliation are prepared.
- `RuntimeRecoveryService` separates safe crash continuation from approval/manual/reconciliation/error states and never blindly replays unresolved side effects.
- Durable start commits Nika `READY -> RUNNING` plus its initial task→runtime recovery cursor atomically.

## Current cycle — atomic local runtime finalization
A second crash-consistency boundary was found and IMPLEMENTED/PREPARED.

Previously `_finish()` mutated the runtime-session cursor, task state and audit log in separate SQLite transactions. A late session or audit failure could therefore expose a partially finalized Nika state.

Changes:
- `AuditLog.append_with_connection()` supports audit evidence inside a caller-owned SQLite transaction.
- `RuntimeSessionStore.record_result_with_connection()` and `delete_with_connection()` support transaction-owned finalization.
- `TaskRuntimeCoordinator._finish()` now commits the Nika runtime-session mutation, task transition, runtime events and final `runtime.finished` audit evidence in one local SQLite transaction.
- If session mutation, task transition, event serialization/write or final audit write fails, the complete Nika finalization rolls back and the previous ACTIVE recovery cursor/task state remain available for explicit recovery.

Prepared fault-injection coverage in `tests/test_runtime_crash_consistency.py` now includes:
1. durable-start failure rolls back both task transition and initial cursor;
2. a fresh start cannot overwrite an existing recovery cursor;
3. terminal session-delete failure rolls back the whole local finalization;
4. terminal final-audit failure rolls back task terminalization and cursor deletion;
5. resumable WAITING_APPROVAL final-audit failure rolls back the wait-state/new checkpoint cursor and preserves the previous ACTIVE recovery cursor.

These M2 tests are committed but are **not yet claimed PASSED** until the reconciled M2 branch executes its full hosted suite.

## M2 synchronization with green M1
The M2 branch has been updated with the green M1 behavior that changed after its historical base:
- proven Action Registry/keymap normalization and regression tests;
- proven workspace entry-point import form;
- shared `scripts/verify.py` harness and README verification instructions;
- M2 CI installs `.[dev,agent]` and then runs the same full verification harness;
- M2 schema-v3 tests retain runtime/idempotency migration expectations while incorporating the green M1 shortcut tests.

A non-force merge ancestry reconciliation with current `main` is the next repository operation before the exact M2 CI gate.

## Reuse-first digital-worker architecture already recorded
- ADAPT Microsoft UFO² as first Windows computer-use proof candidate.
- REUSE Playwright as deterministic semantic browser baseline.
- ADAPT Browser Use only if it measurably improves on that baseline.
- ADAPT OpenHands SDK/agent-server as first Software Factory coding-worker proof candidate.
- ADAPT Unified Planning for explicit deterministic planning domains.
- REUSE ONNX Runtime for compact specialist inference.

## Infrastructure state
The previous billing/spending/runner-allocation blocker is RESOLVED for current work. CI run 74 executed normally and passed. Do not continue treating old pre-step failures as the current blocker.

## Test truth
- M1 exact candidate SHA `67df93c...`: hosted verification PASSED and is integrated.
- M2 finalization/crash-consistency additions: committed/prepared, NOT YET PASSED on the final reconciled M2 SHA.
- No M2 percentage is credited until its exact integrated candidate is green.

## Truth state
- M0: INTEGRATED / GREEN.
- M1: IMPLEMENTED / INTEGRATED / GREEN; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED/PREPARED including atomic durable start and atomic local finalization; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- Digital-worker reuse architecture: RESEARCHED/DOCUMENTED, not IMPLEMENTED.
- Parallel-development governance: PREPARED on PR #4, not integrated.

## Packaging policy
No EXE this cycle. Windows standalone is built only at milestone/user-test/release gates; heavy browser/coding/vision/model workers remain optional components.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Reconcile `dev/m2-runtime-selection` with green `main` without force-push and preserve all reviewed M2 work.
2. Execute the exact reconciled M2 SHA with `.[dev,agent]` through dependency check, Ruff, compile and the complete pytest suite.
3. Fix any real failures found by CI; specifically require the LangGraph/SQLite durability, crash recovery, approvals, cancellation, timeout/retry, idempotency, startup recovery and new atomic finalization fault-injection tests to pass.
4. Merge PR #3 only on genuine green evidence for the exact head SHA.
5. Only after M2 integration may M3 production implementation begin; independent future lanes remain limited to bounded research/contracts until then.
