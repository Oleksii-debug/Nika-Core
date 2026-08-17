# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Proven weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains 6.0%.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` but is not yet INTEGRATED, so its 10% product weight is not credited.
- M1 candidate implementation scope is approximately 95% complete; the remaining gate is executable CI/test evidence and any defects that evidence reveals. This 95% is not added to final A–Z progress.

## Current milestone
M1 — kernel foundation.

## M1 coherent candidate scope
- Pydantic Settings based typed/versioned configuration;
- backward-compatible `NIKA_DB_PATH` plus explicit/long-form database path configuration;
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
M1 original coherent implementation commit: `1d3c0eaa7293b58ce8765662a0e3efbe35f2f5c9`.
Current PR #2 head after configuration compatibility fix/tests: `1c6f544106c3448a37c7bb3b0bb950f24079dda3`.
PR #2 currently changes 19 files and remains mergeable but unmerged.

## Current blocker — confirmed GitHub billing/Actions infrastructure
PR CI run 32073395855 attempt 2 failed before any workflow step started. GitHub check annotation states: `The job was not started because recent account payments have failed or your spending limit needs to be increased. Please check the 'Billing & plans' section in your settings`.
The job has `runner_id=0`, empty runner name and zero steps. This is account/Actions infrastructure evidence, not a code-test failure. Do not merge or credit M1 until a runner executes Ruff/compile/pytest successfully.

## Reuse decisions verified this cycle
- REUSE Pydantic Settings for typed environment configuration. Current official Pydantic Settings docs support validation aliases/AliasChoices and explicit environment prefixes; the M1 configuration preserves the legacy `NIKA_DB_PATH` name while accepting a long-form name and explicit constructor values.
- REUSE Python `sqlite3` for local transactional storage and thin ordered migrations at the current schema size.
- REUSE Python `importlib.metadata.entry_points()` for installed workspace discovery without eager imports.
- CUSTOM thin Nika-specific registries/audit/action/keymap policy because they encode product versioning, safety and accessibility semantics.
- Agent runtime remains intentionally unlocked: M2 will compare current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort`.

## Packaging policy
No EXE for this foundation cycle. Development remains Python/source-first. Windows standalone is built at milestone/user-test/release gates; final product must run without Python.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation. Oleksii performs human NVDA acceptance for relevant candidates.

## Next large coherent batch
First priority: obtain executable PR #2 CI after GitHub Billing & plans / Actions spending is restored. Then inspect exact Ruff/compile/pytest evidence and fix any real defects in the same M1 branch. If green, integrate M1 and immediately start one large M2 runtime-selection proof comparing current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort` with the same durable restart/resume/approval scenario.
