# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-18
Mode: PARALLEL-FIRST
Canonical progress evidence: merged PR + exact green CI + milestone acceptance evidence.

## Operating rule
Nika Core development is not roadmap-sequential. Every autonomous cycle scans the full roadmap and advances independent large lanes when they can be isolated safely. Dependencies constrain integration order, not research, contract design, implementation behind stable ports, fixtures, mocks, tests, documentation or proof prototypes.

A lane may be marked only with these evidence states:
- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — source/tests exist on a branch.
- GREEN — exact branch/PR head passed required automated checks.
- INTEGRATED — exact green candidate merged into main.
- PACKAGED — installable Windows artifact built and checked.
- HUMAN_TESTED — a person completed the specified manual test.
- NVDA_VERIFIED — human NVDA test passed; automation may never award this state.

## Active parallel lanes

### L1 — M3 durable memory, scheduler, resource control
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI 98; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.

### L2 — M4 Model Gateway, tools and MCP
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI 112; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.

### L3 — M5 accessible Windows UI
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI 137; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
Human truth: M5 diagnostic package is not release PACKAGED; HUMAN_TESTED=false; NVDA_VERIFIED=false.

### L4 — M6 Agent Builder and permissions
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI 142; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
Safety truth: configuration activation approval never substitutes for execution-time high-impact tool approval.

### L5 — M7 multi-agent laboratory
Ownership: `src/nika_core/multi_agent`, team persistence, supervisor/child contracts, typed handoffs and quota tests.
Goal: durable delegation, bounded parallel children, depth/concurrency quotas, privilege attenuation and parent/child evidence.
Integration dependency: M2 and M6 integrated — satisfied.
Branch: `dev/m7-multi-agent-lab`.
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `4feb976faa97949bacc321bcbce792d01359a58c`.
Acceptance evidence: Core CI run 150 passed shared dependency consistency, Ruff, compile and complete pytest verification on Ubuntu and Windows; PR #17 merged as `5a01692c1372375f040cd38558e33204b082d5a5`.
Integrated evidence: SQLite v6 durable team/member/handoff/result tables; stable parent-child identity; typed task/result/status/error handoffs; persisted depth/children/total/concurrency quotas; fail-closed privilege attenuation using M6 ToolGrant; bounded parallel fan-out behind AgentRuntimePort; worker-failure containment; cancellation propagation; restart-safe thread/resume evidence; deterministic evaluator aggregation.

### L6 — M8 controlled learning and experiments
Ownership: `src/nika_core/experiments`, strategy/eval/replay models and migrations/tests.
Goal: experiment/run/metric/dataset evidence, replay evaluation, champion/challenger promotion and rollback without silent production-source rewriting.
Integration dependency: current runtime/model/memory/scheduler/resource contracts are integrated and sufficient to implement the deterministic core.
Current state: PREPARED / NEXT WEIGHTED MILESTONE.
Next batch: create versioned immutable experiment/strategy definitions, run and metric persistence, dataset/replay references, deterministic evaluation/comparison, explicit promotion and rollback gates, restart-safe evidence and tests. Learning remains simulation/evaluation only and may never silently rewrite production source or widen permissions.

### L7 — M9 plugin/workspace SDK and real workspaces
Ownership: `src/nika_core/plugins`, `src/nika_core/workspaces`, SDK manifests/contracts and compatibility tests.
Goal: versioned plugin/workspace lifecycle and capability declarations; first proof workspaces are Software Factory and Accessibility Rescue.
Current state: PREPARED FOR IMPLEMENTATION.

### L8 — M10 security/sandbox/reliability
Ownership: `src/nika_core/security`, sandbox/risk/approval policy, public-repository hygiene and security tests.
Goal: enforce R0–R4 policy, least privilege, sandbox boundaries, audit evidence, secret hygiene, failure containment.
Current state: PREPARED FOR IMPLEMENTATION.

### L9 — M11 Windows packaging/distribution
Ownership: `packaging/`, build workflow/specs, release metadata and smoke-test harness.
Goal: reproducible standalone Windows candidate without bundling heavy optional models/workers.
Current state: PREPARED FOR IMPLEMENTATION. M5 produced a diagnostic one-dir package only for accessibility acceptance; it is not M11 release credit.

### L10 — M12 full-system QA/accessibility/release gate
Ownership: `qa/`, cross-lane integration fixtures, Windows/NVDA manual protocol, recovery/security regression matrix.
Goal: keep final QA executable continuously instead of postponing all testing until the end.
Current state: PREPARED FOR IMPLEMENTATION.

## Collision policy
1. Every lane branches from the latest green `main` unless it genuinely depends on another unmerged lane.
2. Do not stack unrelated branches.
3. Prefer new lane-owned modules and stable ports over edits to shared files.
4. Shared contract changes require an explicit compatibility decision and targeted cross-lane tests.
5. A blocked lane never blocks independent lanes.
6. Merge/integration remains dependency ordered even when implementation is parallel.

## CI policy
- Standard public GitHub-hosted runners may be used for coherent PR/main gates.
- Prefer parallel independent jobs and Windows/Linux proof where it adds evidence.
- Never weaken checks to make a lane green.
- Avoid repeated reruns of an identical deterministic failure; inspect and repair first.
- Do not claim product-weight progress for PREPARED/IMPLEMENTED work.

## Reporting policy
Every development report should show confirmed A–Z product progress, lane state changes, blockers, integrated evidence and what still lacks Windows, human or NVDA proof.
