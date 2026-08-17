# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains 6.0%.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 runtime-selection preparation is IMPLEMENTED on `dev/m2-runtime-selection` but not INTEGRATED and receives no final percentage credit yet.

## Current milestone
M1 integration gate is blocked by GitHub Actions account billing/spending infrastructure. Parallel safe preparation for M2 is active without bypassing the M1 merge gate.

## M1 candidate
PR #2: typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Current PR head before M2 branch: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.

## Current blocker
GitHub Actions PR jobs continue to fail before any workflow step starts. The prior exact check annotation identified recent account payment failure or Actions spending-limit configuration. No runner/steps means this is not code-test evidence. Do not merge PR #2 or credit M1 until Ruff/compile/pytest actually execute successfully.

## M2 preparation completed this cycle
- fresh official-source comparison of LangGraph and Microsoft Agent Framework;
- LangGraph selected as primary current runtime for Nika's local Windows/SQLite target;
- Microsoft Agent Framework retained as a secondary adapter candidate;
- framework-neutral `AgentRuntimePort`, request/result/event/outcome contracts;
- capability-based `RuntimeRegistry`;
- deterministic no-LLM `ReferenceRuntime`;
- dated runtime selection evidence matrix;
- deterministic contract/registry/selection tests;
- detailed `docs/RUNTIME_SELECTION.md` with executable durable-resume/approval proof required before M2 credit.

## Runtime reuse decision
ADAPT LangGraph; REUSE `langgraph-checkpoint-sqlite`; KEEP Microsoft Agent Framework as secondary candidate. Selection rationale: LangGraph v1 stable runtime plus direct official SQLite checkpoint package is the closest match to Nika's single-machine durable desktop requirement. Microsoft core is production/stable and workflows are strong, but native Python Ollama integration is currently prerelease and local persistence is less directly SQLite-aligned.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2 runtime boundary/selection: IMPLEMENTED on dependent branch, not INTEGRATED, executable framework proof not yet run.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
First re-check Actions infrastructure. If executable CI becomes available, run PR #2 and fix any real defect before merge. Then rebase/retarget the M2 runtime branch onto green main and implement the actual LangGraph SQLite durable adapter proof: persisted restart/resume, approval interrupt/resume, cancellation and Nika audit/task-state mapping. If Actions remains billing-blocked, continue that proof on the dependent dev branch but do not merge or credit progress without executable evidence.
