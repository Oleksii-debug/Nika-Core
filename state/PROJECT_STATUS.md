# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- M2 durable agent runtime: GREEN / INTEGRATED, 100% of its 11% weight.
- Overall proven final A–Z product progress is now **27.0%**.

## Current milestone
M2 is closed. M3 — memory, scheduler and resource control — is the next production integration milestone. Independent future lanes may continue bounded research/contracts in parallel, but no later milestone receives product credit before its own acceptance gate.

## Proven M1 evidence
- PR #2 exact green head: `67df93c355e813dfc297bd1111df40d3c4ad6175`.
- GitHub Actions `Core CI` run `32133041861` (run 74) completed `success` on that exact head.
- PR #2 merged into `main` as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.

## Proven M2 evidence
- PR #3 exact green head: `c890a5eadbea01afe92617f440ca83005c3b5f0c`.
- GitHub Actions `Core CI` run `32134139940` (run 85) completed `success` on that exact head.
- The job successfully executed checkout, Python setup, `.[dev,agent]` dependency installation and the shared `python scripts/verify.py` harness.
- The full verification gate therefore passed dependency consistency, Ruff, Python compilation and the complete pytest suite on the exact candidate.
- PR #3 merged into `main` as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M2 therefore earns its full 11% acceptance-gate weight.

## M2 integrated capability
- Framework-neutral `AgentRuntimePort`; LangGraph is the primary implemented runtime while Microsoft Agent Framework remains a secondary research/migration alternative.
- Async local durability through LangGraph `AsyncSqliteSaver` + `aiosqlite` with strict checkpoint deserialization.
- Persisted Nika task→runtime session routing and restart recovery.
- Explicit human-approval resume boundary; ordinary continuation cannot silently grant approval.
- Cancellation, execution timeout and bounded retry policy.
- Persistent idempotency/reconciliation ledger for potentially duplicated external side effects.
- Startup recovery classifies safe crash continuation separately from approval, manual resume, unresolved side effects, missing runtime and inconsistent state.
- Durable start commits `READY -> RUNNING` plus the initial recovery cursor atomically.
- Runtime finalization commits session mutation/deletion, task state transition, normalized runtime events and final audit evidence atomically in one Nika SQLite transaction.
- Fresh starts cannot overwrite an existing persisted recovery cursor.

## Crash-consistency evidence included in the green M2 suite
Deterministic fault-injection coverage includes:
1. durable-start failure rolls back both task transition and initial cursor;
2. a fresh start cannot overwrite an existing recovery cursor;
3. terminal session-delete failure rolls back the whole local finalization;
4. terminal final-audit failure rolls back task terminalization and cursor deletion;
5. resumable WAITING_APPROVAL final-audit failure rolls back the wait-state/new checkpoint cursor and preserves the previous ACTIVE recovery cursor.

Real LangGraph + SQLite tests also cover persistent approval/resume and the integrated coordinator path. Startup recovery tests verify that only clean crash sessions auto-resume while unresolved external side effects remain blocked for reconciliation.

## CI repair history from this integration cycle
The old account/billing/runner-allocation blocker is RESOLVED and must not be treated as current.

The first executable M2 CI exposed Ruff defects; those were fixed rather than bypassed. The next run passed dependency check, Ruff and compile but found 4 pytest contract issues: stale schema-v2 expectation, an audit expectation missing the new durable `runtime.session_bound` event, and two async tests that depended on an undeclared pytest plugin. These were repaired without weakening production checks or adding an unnecessary dependency. Run 85 then passed the complete gate.

## Reuse-first digital-worker architecture already recorded
- ADAPT Microsoft UFO² as first Windows computer-use proof candidate.
- REUSE Playwright as deterministic semantic browser baseline.
- ADAPT Browser Use only if it measurably improves on that baseline.
- ADAPT OpenHands SDK/agent-server as first Software Factory coding-worker proof candidate.
- ADAPT Unified Planning for explicit deterministic planning domains.
- REUSE ONNX Runtime for compact specialist inference.

## Truth state
- M0: IMPLEMENTED / INTEGRATED / GREEN; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / INTEGRATED / GREEN; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / INTEGRATED / GREEN; not PACKAGED; not HUMAN_TESTED.
- M3+: not yet credited as integrated production milestones.
- Digital-worker reuse architecture: RESEARCHED/DOCUMENTED; production implementation remains milestone-gated.
- Parallel-development governance: PREPARED on PR #4 unless/until separately integrated.

## Packaging policy
No EXE this cycle. Windows standalone is built only at milestone/user-test/release gates; heavy browser/coding/vision/model workers remain optional components.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next LARGE coherent batch
Primary M3 production lane:
1. reread current `main` and perform reuse audit for durable memory, scheduling and local resource-control primitives;
2. define versioned ports/contracts for task/agent/workspace/user-approved memory, scheduler jobs and resource budgets without binding the domain to a specific backend;
3. implement the largest coherent SQLite-backed memory/scheduler/resource slice that can reach an acceptance boundary in one development branch;
4. include migrations, deterministic expiration/retention semantics, cancellation-safe scheduling, resource-limit enforcement, audit evidence and restart proofs;
5. run the shared verification harness and integrate M3 work only on exact green evidence.

Parallel independent lanes may research Computer Interaction Layer, Software Factory, offline-minimal intelligence and M5 UI reuse candidates, but they must remain isolated from M3 integration dependencies and cannot bypass milestone gates.
