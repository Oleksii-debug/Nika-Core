# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT / PARALLEL-FIRST

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- M2 durable agent runtime: GREEN / INTEGRATED, 100% of its 11% weight.
- Overall proven final A–Z product progress is **27.0%**.

No later milestone receives product-weight credit until its own acceptance gate passes, even when multiple later lanes are being implemented simultaneously.

## Proven M1 evidence
- PR #2 exact green head: `67df93c355e813dfc297bd1111df40d3c4ad6175`.
- GitHub Actions `Core CI` run 74 completed `success` on that exact head.
- PR #2 merged into `main` as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.

## Proven M2 evidence
- PR #3 exact green head: `c890a5eadbea01afe92617f440ca83005c3b5f0c`.
- GitHub Actions `Core CI` run 85 completed `success` on that exact head.
- The job executed checkout, Python setup, `.[dev,agent]` dependency installation and `python scripts/verify.py`.
- PR #3 merged into `main` as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M2 therefore earns its full 11% acceptance-gate weight.

## M2 integrated capability
- Framework-neutral `AgentRuntimePort`; LangGraph is the primary implemented runtime while Microsoft Agent Framework remains a migration/interop candidate.
- Async local durability through LangGraph `AsyncSqliteSaver` + `aiosqlite` with strict checkpoint deserialization.
- Persisted Nika task→runtime session routing and restart recovery.
- Explicit human-approval resume boundary; ordinary continuation cannot silently grant approval.
- Cancellation, execution timeout and bounded retry policy.
- Persistent idempotency/reconciliation ledger for potentially duplicated external side effects.
- Startup recovery classifies safe crash continuation separately from approval, manual resume, unresolved side effects, missing runtime and inconsistent state.
- Durable start commits `READY -> RUNNING` plus the initial recovery cursor atomically.
- Runtime finalization commits session mutation/deletion, task state transition, normalized runtime events and final audit evidence atomically.

## CI repair history
The old private-repository runner/minute blocker is RESOLVED and must not be treated as current. After the repository became public, executable CI exposed real source/test defects; those were repaired without weakening checks. M1 and M2 then passed exact green gates.

## Parallel-development governance
- PR #4 `Parallel Development Policy` is MERGED and is no longer merely prepared.
- `docs/PARALLEL_DEVELOPMENT_POLICY.md` is binding repository governance.
- Development scans the entire M3–M12 backlog and normally advances 5–10 independent large lanes.
- Dependencies constrain integration order, not isolated implementation, research, fixtures, mocks, tests or prototypes.
- Unrelated work must branch from the latest green `main`; unrelated branches must not be stacked.
- See `state/PARALLEL_EXECUTION_BOARD.md` for lane ownership and evidence states.

## Public-repository hardening now in flight
- secret/credential/session/cookie/private-state ignore patterns are being strengthened;
- current-tree searches for common secret names have not identified an actual credential value;
- public visibility means secret hygiene is now a permanent gate, including history-aware scanning when the appropriate scanner is added.

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
- M3–M12: parallel source lanes may be PREPARED/IMPLEMENTED/GREEN independently; none is credited INTEGRATED until its own gate.
- Parallel-development governance: INTEGRATED through PR #4.
- Digital-worker reuse architecture: RESEARCHED/DOCUMENTED; production adapters remain milestone-gated.
- No Windows standalone package yet.
- No human NVDA verification yet.

## Parallel CI acceleration
The CI control-plane change under review runs the same verification harness independently on Ubuntu and Windows with fail-fast disabled, so one operating-system failure does not hide evidence from the other. Stale runs for the same PR/ref are cancelled to avoid wasting runner capacity.

## Active development wave
Ten lanes are opened conceptually in `state/PARALLEL_EXECUTION_BOARD.md`:
1. M3 durable memory/scheduler/resource control;
2. M4 Model Gateway/tools/MCP;
3. M5 accessible Windows UI;
4. M6 Agent Builder/permissions;
5. M7 multi-agent laboratory;
6. M8 controlled learning/experiments;
7. M9 plugin/workspace SDK;
8. M10 security/sandbox/reliability;
9. M11 Windows packaging/distribution;
10. M12 continuous full-system QA/accessibility/release gates.

The immediate execution rule is not “finish M3, then start M4”. Each cycle advances independent large slices from as many of these lanes as can be safely isolated, while merge order and product-credit rules remain strict.
