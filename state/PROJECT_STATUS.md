# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains 6.0%.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 runtime-selection/adapter/integration preparation is IMPLEMENTED on `dev/m2-runtime-selection` but not INTEGRATED and receives no final percentage credit yet.

## Current milestone
M1 integration gate is blocked by GitHub Actions account billing/spending infrastructure. Parallel safe preparation for M2 is active without bypassing the M1 merge gate.

## M1 candidate
PR #2: typed/versioned configuration, SQLite migration v1→v2, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. Current PR head before M2 branch: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.

## Current blocker
GitHub Actions PR jobs continue to fail before any workflow step starts. The prior exact check annotation identified recent account payment failure or Actions spending-limit configuration. No runner/steps means this is not code-test evidence. Do not merge PR #2 or credit M1 until Ruff/compile/pytest actually execute successfully.

## M2 large coherent batch completed this cycle
Dependent branch: `dev/m2-runtime-selection`.
Current branch head before this status update: `f1241a4ef9f83bfd9545fc6bfa3397b0723e5c18`.

Implemented/prepared:
- fresh official-source comparison of LangGraph and Microsoft Agent Framework;
- LangGraph selected as primary current runtime for Nika's local Windows/SQLite target;
- Microsoft Agent Framework retained as a secondary adapter candidate;
- framework-neutral `AgentRuntimePort` with explicit start/resume/cancel surface;
- normalized request/resume/event/result/outcome contracts and resume modes;
- both initial execution and resume carry explicit positive max-step limits;
- capability-based `RuntimeRegistry`;
- deterministic no-LLM `ReferenceRuntime`;
- dated runtime selection evidence matrix;
- thin `LangGraphRuntime` adapter that normalizes completed results, failures and human-approval interrupts without leaking framework object types into Nika callers;
- Nika max-step limits map to LangGraph per-run recursion limits;
- explicit ordinary continuation versus approval continuation behavior;
- cancellation deliberately not advertised until a real behavior proof exists;
- `open_langgraph_sqlite()` secure local checkpoint boundary with explicit connection lifecycle, `check_same_thread=False`, saver setup and strict MsgPack deserialization forced on at the Nika boundary even if the environment previously requested an insecure value;
- `TaskRuntimeCoordinator` maps runtime results into Nika TaskQueue states and AuditLog evidence;
- deterministic tests for runtime contracts, registry selection, adapter normalization, approval resume, bounded execution, output isolation, failure normalization, truthful capabilities, SQLite helper security/lifecycle, task-state mapping and audit mapping;
- expanded `docs/RUNTIME_SELECTION.md` with exact real-framework durability proof still required before M2 credit.

## Runtime reuse decision
ADAPT LangGraph; REUSE `langgraph-checkpoint-sqlite`; KEEP Microsoft Agent Framework as secondary candidate. Selection rationale: LangGraph v1 stable runtime plus direct official SQLite checkpoint package is the closest match to Nika's single-machine durable desktop requirement. Microsoft core is production/stable and workflows are strong, but native Python Ollama integration is currently prerelease and local persistence is less directly SQLite-aligned.

## Exact evidence and limitations
- GitHub branch history contains the runtime contracts, selection evidence, LangGraph adapter, secure checkpoint helper, coordinator and associated deterministic tests/documents.
- Hosted CI cannot currently execute because of the account Billing/Actions blocker.
- Therefore no claim is made that Ruff/compile/pytest or the real LangGraph package durability proof passed this cycle.
- Manual source review in this cycle caught and removed an unused import before CI and corrected two contract risks: an ignored max-step limit and insecure strict-deserialization override behavior.
- No M1 or M2 progress weight is credited without executable evidence.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2 runtime boundary/selection/adapter/coordinator: IMPLEMENTED on dependent branch, not INTEGRATED, real-framework durable proof not yet executed, not PACKAGED, not HUMAN_TESTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
First re-check Actions infrastructure. If executable CI becomes available, run PR #2 and fix any real defect before merge. Then retarget/rebase the M2 runtime branch onto green main and execute the real LangGraph SQLite durability proof: persist a completed step, destroy/recreate runtime/checkpointer objects, resume without repeating completed work, approval interrupt/persist/recreate/resume, invalid/corrupt checkpoint fail-closed behavior, real TaskRuntimeCoordinator TaskQueue/AuditLog mapping and cancellation semantics before adding the cancellation capability. If Actions remains billing-blocked, continue that proof on the dependent dev branch but do not merge or credit progress without executable evidence.
