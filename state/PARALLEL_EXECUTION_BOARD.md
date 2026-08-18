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
Branch: `dev/m3-memory-scheduler-resources` from main `58a5fbf708493b64cb76ed6541928e2f17ae6bc8`.
Current state: IMPLEMENTED / CI NOT YET PROVEN.
Prepared implementation baseline before status-only commits: `ac57ec1524ba8e4efec944e3ef3acd12ee865e81`.
Evidence prepared: migration v4, durable scoped memory, user-consent gate, expiration, APScheduler rehydration/pause-resume, persisted resource budgets, psutil observation, FIFO resource fairness and tests.

### L2 — M4 Model Gateway, tools and MCP
Ownership: `src/nika_core/model_gateway`, `src/nika_core/tools`, provider/tool adapter tests.
Goal: provider-neutral local/cloud/no-LLM gateway, standardized tool calls, cancellation/timeouts/audit, MCP boundary.
Integration dependency: stable kernel/runtime contracts; source work can proceed now.
Current state: PREPARED FOR IMPLEMENTATION.

### L3 — M5 accessible Windows UI
Ownership: `ui/`, Windows WebView host adapter, accessibility contracts/tests.
Goal: keyboard-first Tasks/Agents/Workspaces/Logs/Settings shell with Action Registry/Keymap integration and explicit accessible names/status text.
Integration dependency: backend APIs may be mocked; real NVDA acceptance remains human-only.
Current state: PREPARED FOR IMPLEMENTATION.

### L4 — M6 Agent Builder and permissions
Ownership: `src/nika_core/builder`, agent-definition compiler/validation, permission/risk models.
Goal: transform a natural-language agent request into a deterministic versioned definition with tools, schedule, model, budgets and safety boundaries.
Integration dependency: model/tool execution may be mocked behind ports.
Current state: PREPARED FOR IMPLEMENTATION.

### L5 — M7 multi-agent laboratory
Ownership: `src/nika_core/multi_agent`, supervisor/child contracts, typed messages and quota tests.
Goal: durable delegation, bounded parallel children, depth/concurrency quotas, privilege attenuation and parent/child evidence.
Integration dependency: builds on AgentRuntimePort; M2 already integrated.
Current state: PREPARED FOR IMPLEMENTATION.

### L6 — M8 controlled learning and experiments
Ownership: `src/nika_core/experiments`, strategy/eval/replay models and migrations/tests.
Goal: experiment/run/metric/dataset records, replay evaluation, champion/challenger promotion and rollback without silent production-source rewriting.
Integration dependency: can implement deterministic engine against ports before all production tools exist.
Current state: PREPARED FOR IMPLEMENTATION.

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
Current state: PREPARED FOR IMPLEMENTATION.

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
