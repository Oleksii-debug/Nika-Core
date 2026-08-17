# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Proven weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains 6.0%.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` but is not yet INTEGRATED, so its 10% product weight is not credited.

## Current milestone
M1 — kernel foundation.

## M1 coherent candidate scope
- Pydantic Settings based typed/versioned configuration;
- ordered SQLite migrations from schema 1 to schema 2 and future-schema fail-closed behavior;
- persisted versioned Agent Registry;
- persisted versioned Workspace Registry;
- generic deterministic Audit Log;
- standard Python entry-point workspace discovery contract;
- central Action Registry;
- persisted user Keymap with remap/unbind/restore/import/export/conflict detection;
- existing task/checkpoint behavior retained;
- updated architecture/reuse documentation removing stale UI/runtime assumptions.

## Exact evidence
Main green baseline: `df48f70b738f9227cad1df08ce3d7f40115b5f08` — Core CI SUCCESS.
M1 coherent implementation commit: `1d3c0eaa7293b58ce8765662a0e3efbe35f2f5c9`.
PR: #2, 19 changed files, 821 additions, 155 deletions.

## Current blocker
PR CI run 32072735859 failed twice before any workflow step started. Both attempts show no allocated runner and no executed steps, so there is currently no code-level failure evidence and no green integration evidence. Do not merge or credit M1 until a runner executes Ruff/compile/pytest successfully. Treat as infrastructure/transient unless later evidence identifies a repository configuration/billing/runner problem.

## Reuse decisions
REUSE Pydantic Settings; REUSE Python sqlite3; CUSTOM thin ordered migration runner; REUSE Python importlib.metadata entry points; CUSTOM Nika-specific registries/audit/action/keymap policy. Agent runtime is intentionally not locked yet: M2 will compare current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort`.

## Packaging policy
No EXE for this foundation cycle. Development remains Python/source-first. Windows standalone is built at milestone/user-test/release gates; final product must run without Python.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation. Oleksii performs human NVDA acceptance for relevant candidates.

## Next large coherent batch
First priority next cycle: re-check PR #2 runner/CI. If CI starts and reports code defects, fix them before new functionality. When M1 is green and integrated, move immediately to one large M2 runtime-selection proof comparing current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort` with the same durable restart/resume/approval scenario.
