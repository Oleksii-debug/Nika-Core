# Nika Core — Multi-Plan Parallelism Contract

## Shared architecture laws
- Windows-first modular monolith with presentation-neutral application boundaries.
- REUSE -> ADAPT -> CUSTOM(thin).
- One canonical persistence/runtime/security/scheduler/audit/Capability Broker/ModelGateway authority.
- Models are replaceable capabilities, not the kernel.
- Agent orchestration remains behind AgentRuntimePort.
- No secret material in prompts/models/Git/logs/artifacts.
- Permission/effect/recovery truth is deterministic and durable.
- Windows/NVDA and Web accessibility evidence remain distinct.

## Ownership
### Plan 1 — Core platform
Owns old Sections 1–10: architecture/contracts/config/persistence/runtime/security/scheduler/audit/capability broker/model gateway.
Conflict keys: core-contracts, persistence-runtime, security-policy, scheduler, capability-model-gateway.

### Plan 2 — Agent/Product Factory
Owns old Sections 11–12 and 15–22: Agent Builder, teams, CodingWorker, ProductProject, Research-to-Product, Team Composer, repo graph, build/execution, QA, deploy/ops/continuity, representative Factory.
Conflict keys: agent-platform, product-project, product-factory.

### Plan 3 — Intelligence/research/learning
Owns old Sections 23–30.
Conflict keys: deterministic-brain, model-intelligence, memory-knowledge, research-learning.

### Plan 4 — Windows/interaction/accessibility/media
Owns old Sections 13–14 and 31–36.
Conflict keys: windows-shell, browser-uia, accessibility, media-report.

### Plan 5 — Domain workspaces
Owns old Sections 37–39: AI Trader, Personal Nika/Living Agent, Autonomous Business Factory.
May use frozen interfaces/fixtures from Plans 1–4; must not fork their authorities.
Conflict keys: trader-workspace, living-agent, business-factory.

### Plan 6 — Web/Cloud/release infrastructure
Owns old Sections 40–45: shared Web/Cloud API/runtime, accessible Web/multi-tenant/account layer, Nika Node, Windows package/update, supply-chain provenance, diagnostics/performance/support.
Conflict keys: web-cloud, node-bridge, packaging-supply-chain, observability-performance.

### Plan 7 — final convergence
Owns old Sections 46–49 only. It integrates, physically qualifies and releases; a generic missing subsystem returns to its owning Plan 1–6.
Conflict key: final-convergence.

## Shared-file rule
Prefer additive/versioned contracts, refresh main before integration, declare conflict key, converge one shared mutation rather than serializing all plans.

## Evidence classes
Component source/fixtures, packaged Windows, physical NVDA, real Web/browser, real Cloud/Node and final release evidence are separate and never interchangeable.
