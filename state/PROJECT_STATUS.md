# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Proven weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product before M1 integration evidence: 6.0%.
- M1 candidate implementation is on `dev/m1-foundation`; do not credit its 10% weight until exact PR/main CI is green and integrated.

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

## Current exact baseline before M1 integration
Main: `df48f70b738f9227cad1df08ce3d7f40115b5f08` — GitHub Actions Core CI SUCCESS.
M1 branch: `dev/m1-foundation` — exact candidate SHA is recorded after the coherent commit.

## Reuse decisions
REUSE Pydantic Settings; REUSE Python sqlite3; CUSTOM thin ordered migration runner; REUSE Python importlib.metadata entry points; CUSTOM Nika-specific registries/audit/action/keymap policy. Agent runtime is intentionally not locked yet: M2 will compare current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort`.

## Packaging policy
No EXE for this foundation cycle. Development remains Python/source-first. Windows standalone is built at milestone/user-test/release gates; final product must run without Python.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation. Oleksii performs human NVDA acceptance for relevant candidates.

## Next large coherent batch after M1 is green
M2 architecture selection + durable runtime proof: implement the same restart/resume/approval scenario behind `AgentRuntimePort` using current LangGraph and Microsoft Agent Framework, compare evidence, select one primary runtime, then integrate the winning adapter without leaking framework types into Nika domain APIs.
