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
Ownership: `src/nika_core/memory`, `src/nika_core/scheduler`, `src/nika_core/resources`, migrations/tests.
Goal: restart-safe scoped memory and schedules with deterministic retention/expiration, cancellation and resource budgets/fairness.
Integration dependency: M0–M2 integrated — satisfied.
Branch: `dev/m3-memory-scheduler-resources`.
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `c9c7e105838d9af8a65341fd28f4591aee0d851c`.
Acceptance evidence: Core CI run 98 passed shared verification on Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
Integrated evidence: migration v4, durable scoped memory, user-consent gate, expiration, APScheduler rehydration/pause-resume, persisted resource budgets, psutil observation, FIFO resource fairness and regression tests.

### L2 — M4 Model Gateway, tools and MCP
Ownership: `src/nika_core/model_gateway`, `src/nika_core/tools`, provider/tool adapter tests.
Goal: provider-neutral local/cloud/no-LLM gateway, standardized tool calls, cancellation/timeouts/audit, MCP boundary.
Integration dependency: M0–M3 integrated — satisfied.
Branch: `dev/m4-model-tools-mcp`.
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`.
Acceptance evidence: Core CI run 112 passed shared Ubuntu + Windows verification and the focused live Ollama provider proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
Integrated evidence: stable model/provider contracts, deterministic no-LLM + OpenAI-compatible + Ollama adapters, typed timeout/cancellation/provider errors, guarded tool execution, official MCP SDK v2 discovery/call adapter, in-process MCP proof and fail-closed risky-call approval regression.

### L3 — M5 accessible Windows UI
Ownership: `src/nika_core/ui`, Windows WebView host adapter, accessibility contracts/tests.
Goal: keyboard-first Tasks/Agents/Workspaces/Logs/Keyboard shell with Action Registry/Keymap integration and explicit accessible names/status/focus semantics.
Integration dependency: M0–M4 integrated — satisfied.
Branch: `dev/m5-accessible-ui`.
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`.
Acceptance evidence: Core CI run 137 passed shared Ubuntu verification, shared Windows verification, and packaged PyInstaller one-dir WebView2 UI Automation + keyboard/focus proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
Integrated evidence: pywebview + explicit EdgeChromium/WebView2 shell, supported local-path hosting, narrow validated bridge, centralized configurable Action Registry/Keymap, semantic HTML/live status, deterministic focus targets, packaged UIA descendant discovery, `Alt+1` navigation focus and `Ctrl+Shift+P` command focus proof.
Human truth: diagnostic packaged proof does not equal release PACKAGED; HUMAN_TESTED=false; NVDA_VERIFIED=false.

### L4 — M6 Agent Builder and permissions
Ownership: `src/nika_core/builder`, agent-definition compiler/validation, permission/risk models.
Goal: transform a natural-language agent request into a deterministic versioned definition with tools, schedule, model, budgets and safety boundaries.
Integration dependency: M2 runtime + M4 model/tool contracts are integrated — satisfied.
Branch: `dev/m6-agent-builder-permissions`.
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`.
Acceptance evidence: Core CI run 142 passed the complete shared verification on Ubuntu and Windows; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
Integrated evidence: versioned strict Pydantic agent definitions and JSON Schema; Model Gateway drafting with strict post-validation; deterministic registry-backed model/schedule/resource/tool compilation; fail-closed R0–R4 risk matching; persisted compiled R4 approvals; migration v5 immutable versions; one-active-version invariant; atomic retirement/activation/audit; approval-bypass and mutation regressions.
Safety truth: configuration activation approval never substitutes for execution-time high-impact tool approval.

### L5 — M7 multi-agent laboratory
Ownership: `src/nika_core/multi_agent`, supervisor/child contracts, typed messages and quota tests.
Goal: durable delegation, bounded parallel children, depth/concurrency quotas, privilege attenuation and parent/child evidence.
Integration dependency: builds on AgentRuntimePort; M2 and M6 are now integrated.
Current state: PREPARED / NEXT WEIGHTED MILESTONE.
Next batch: reuse LangGraph graph/subgraph durability behind Nika-owned supervisor/child contracts; implement durable parent-child identity, typed handoffs/results, bounded fan-out, spawn depth/concurrency limits, privilege attenuation, cancellation propagation, restart-safe evidence and evaluator aggregation with deterministic acceptance tests.

### L6 — M8 controlled learning and experiments
Ownership: `src/nika_core/experiments`, strategy/eval/replay models and migrations/tests.
Goal: experiment/run/metric/dataset records, replay evaluation, champion/challenger promotion and rollback without silent production-source rewriting.
Integration dependency: can implement deterministic engine against ports before all production tools exist.
Current state: PREPARED FOR IMPLEMENTATION; draft PR #11 may contain candidate experiment work but has no integration credit.

### L7 — M9 plugin/workspace SDK and real workspaces
Ownership: `src/nika_core/plugins`, `src/nika_core/workspaces`, SDK manifests/contracts and compatibility tests.
Goal: versioned plugin/workspace lifecycle and capability declarations; first proof workspaces are Software Factory and Accessibility Rescue.
Integration dependency: adapters may use mocks until computer/browser workers are accepted.
Current state: PREPARED FOR IMPLEMENTATION.

### L8 — M10 security/sandbox/reliability
Ownership: `src/nika_core/security`, sandbox/risk/approval policy, public-repository hygiene and security tests.
Goal: enforce R0–R4 policy, least privilege, sandbox boundaries, audit evidence, secret hygiene, failure containment.
Integration dependency: can proceed independently against current runtime contracts.
Current state: PREPARED FOR IMPLEMENTATION.

### L9 — M11 Windows packaging/distribution
Ownership: `packaging/`, build workflow/specs, release metadata and smoke-test harness.
Goal: reproducible standalone Windows candidate without bundling heavy optional models/workers.
Integration dependency: packaging prototypes may proceed now; release credit waits for integrated feature set.
Current state: PREPARED FOR IMPLEMENTATION. M5 produced a diagnostic one-dir package only for accessibility acceptance; it is not M11 release credit.

### L10 — M12 full-system QA/accessibility/release gate
Ownership: `qa/`, cross-lane integration fixtures, Windows/NVDA manual protocol, recovery/security regression matrix.
Goal: keep final QA executable continuously instead of postponing all testing until the end.
Integration dependency: tests can grow continuously; final human/NVDA gates occur only on packaged candidates.
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
Every development report should show:
- confirmed A–Z product progress;
- lane state changes;
- exact blockers if any;
- what is running/prepared in parallel;
- what was integrated;
- what still lacks Windows, human or NVDA proof.
