# Nika Core — versioned release train

Status: binding release-sequencing clarification for the existing Full Product Vision.

This document does **not** reduce the final Nika Core scope. `docs/MASTER_SPEC.md`, `docs/FULL_PRODUCT_VISION_2026-08-19.md`, `docs/ROADMAP.md`, Product Factory/Business Factory specifications, security boundaries, accessibility gates and release/provenance requirements remain binding.

The purpose of this release train is to create explicit intermediate finishes so the user can receive and use progressively stronger Windows builds while the full product continues to advance in parallel.

## Release-train policy

1. Full-product development continues in parallel. Do not stop independent Product Factory, Toolsmith, security, durability, research, media, model-lab, packaging, governance or other non-colliding work merely because an earlier user release is being sealed.
2. The nearest user-usable release receives higher prioritization when lane choices are otherwise comparable.
3. A release may claim only the capabilities explicitly included in that release. Missing later-version capabilities do not block an earlier release unless they are a dependency of the earlier release's declared user journey.
4. No staged release may weaken R0–R4 approval boundaries, secret handling, provenance, release integrity, accessibility semantics, restart safety or exact-head evidence requirements.
5. Every staged Windows release still follows the Product Journey rule for the capabilities it claims:
   `packaged Windows UI -> semantic action/command -> validated bridge/API -> real Nika service/runtime -> persisted state/result -> accessible visible feedback -> restart/resume where relevant`.
6. `HUMAN_TESTED` and `NVDA_VERIFIED` remain human-only states. Automation never awards them.
7. User data and durable state must migrate forward between staged releases. A new version is an update of the same Nika product, not a separate incompatible application.
8. REUSE -> ADAPT -> CUSTOM (thin) remains mandatory. Staged releases should converge already-built foundations before inventing replacement frameworks.

# V0.1 — Usable Agent Team Alpha

## User outcome

V0.1 is the first release that the user should be able to use as a practical Windows digital-worker application rather than as a backend demonstration.

The defining capability is a **real team of three agents** that can execute long-running timed operational tasks using local or API intelligence and can interact with websites and supported Windows interfaces under Nika policy.

V0.1 is **not** blocked by completion of Autonomous Product Factory, Business Factory, AI Trader, Model Engineering Lab, every media engine, every local-model backend or autonomous software creation.

## Mandatory V0.1 capabilities

### 1. Packaged accessible Windows application

- one installable/portable Windows candidate that does not require the user to manage a Python environment;
- keyboard-operable final UI with semantic controls and accessible names/roles;
- real backend wiring, not placeholder buttons or mock-only lists;
- visible task/team state, logs, errors and results;
- pause/cancel/stop controls for active work;
- packaged WebView2/UIA discovery where applicable;
- final NVDA verification remains a separate human gate.

### 2. Three-agent team execution

A user can create or select a three-agent team and give it one operational goal.

The exact internal roles may vary by task, but the first release must prove a representative supervisor/worker/checker pattern:

- agent A coordinates/decomposes the goal and observes team state;
- agent B performs one independent operational/research/interaction slice;
- agent C performs another slice, monitoring/checking/comparison or verification work;
- agents exchange typed handoffs/results through Nika-owned contracts;
- independent subtasks may run concurrently within configured quotas;
- failure/cancellation of one worker does not corrupt the team or unrelated work;
- child agents never exceed the parent/team permission ceiling.

The product must not hard-code a permanent global agent count of three. Three is the **minimum proven V0.1 representative team journey**; later releases may use dynamic team sizes.

### 3. Local or API AI through ModelGateway

V0.1 must support at least one practical local route and one practical API/cloud-compatible route through the existing provider-neutral ModelGateway contract when configured by the user.

Preferred V0.1 unblockers are existing supported external-local/OpenAI-compatible routes such as Ollama and approved API providers. Foundry Local physical-hardware proof is valuable but must not block V0.1 if an already-supported local route satisfies the declared V0.1 user journey.

Provider secrets remain outside prompts, ordinary logs and durable task state.

### 4. Durable timing, waiting and recurring execution

V0.1 must treat time as a first-class part of an operational workflow. The user can express tasks such as:

- start at a specified clock time;
- wait until a specified time before the next step;
- wait/delay for a bounded duration, then continue;
- repeat every declared interval;
- run on a recurring schedule;
- monitor during a declared time window;
- stop at a declared deadline or when the success condition is met.

Implementation must reuse the existing SchedulerPort/APScheduler direction and durable runtime/checkpoint semantics. Long waits must not depend on a busy loop or one fragile in-memory sleep.

Required timing behavior:

- schedule identity and next intended action survive app restart where the task is declared durable;
- after restart, Nika reconstructs the intended workflow without duplicating already-completed external effects;
- missed-run behavior is explicit: run-late, skip, or fail/block according to declared policy rather than silently inventing behavior;
- pause/cancel prevents future scheduled effects until explicitly resumed;
- retry/idempotency rules remain authoritative for external actions.

### 5. Website monitoring and research

A three-agent team can perform a durable monitoring/search task over one or more user-declared web sources:

- open or revisit declared pages;
- extract relevant semantic content;
- search/filter/compare information;
- detect a meaningful change or condition;
- record evidence/result state;
- continue according to the schedule;
- present an accessible user-visible report or alert state.

Monitoring should avoid unnecessarily reprocessing unchanged content where existing research/change-state capability can be reused.

### 6. Browser tabs and semantic actions

V0.1 must prove a real browser interaction path based on Playwright/semantic browser control before screenshot or coordinate fallbacks.

Representative supported actions include:

- open a page or a new tab;
- switch among task-owned tabs/pages;
- navigate by URL or semantic link;
- locate controls by role/label/name/visible semantic text;
- click a declared button/link;
- enter text in a declared field;
- select a declared option where supported;
- wait for a semantic condition, navigation, content change or bounded timeout;
- read the resulting page state and return evidence to the task;
- close task-owned tabs when appropriate.

The agent must not silently use arbitrary coordinates when semantic DOM/accessibility information is available.

### 7. Supported Windows semantic interaction

V0.1 should expose the already-supported Windows interaction boundary for bounded real tasks where current integrated evidence permits it.

Priority remains:

1. native/application API;
2. Windows UI Automation/accessibility semantics;
3. deterministic named controls;
4. OCR/vision fallback;
5. coordinates only as a last-resort bounded fallback.

V0.1 does not promise universal control of every Windows application. It promises a proven supported semantic Windows interaction path with clear failure when a target is unsupported.

### 8. Permissions, approvals and visible audit

Low-risk observation/navigation can proceed under configured policy. Higher-impact actions remain governed by Nika approval boundaries.

V0.1 must not turn "agent can click" into unrestricted computer control. Send/delete/publish/account/financial/code-execution or other high-impact actions remain previewed/audited/approved according to existing policy.

### 9. Restart/resume

A representative long-running timed three-agent task must survive:

- ordinary application close/reopen;
- restart while waiting for a scheduled future step;
- restart after at least one completed step;
- worker failure where unrelated team state remains valid.

Completed external side effects must not be repeated merely because Nika restarted.

## V0.1 representative acceptance scenario

From the packaged Windows UI, configure local or API intelligence and issue a controlled task equivalent to:

> Use a team of three agents. At a user-specified start time, open two declared web sources in task-owned tabs. Let two workers inspect/search them independently and let the third compare/check the results. If a declared condition is absent, wait until the next scheduled interval and check again. Continue until the declared deadline or condition is met. Show me the current team state and accumulated result. I must be able to pause the task, close Nika, reopen it while it is waiting, and resume without repeating a previously completed external action.

The automated acceptance fixture may use controlled/local web pages so CI is deterministic. A real Windows package then receives packaged smoke/UIA evidence and the separate human/NVDA protocol.

## V0.1 release blockers

V0.1 is blocked by defects in capabilities it claims, including:

- packaged UI not wired to real task/team/runtime state;
- no real three-agent team execution through final UI;
- timing/waiting implemented only as fragile process-local sleeps;
- schedule/restart loses or duplicates work;
- browser actions are mock-only or coordinate-only when semantics exist;
- local/API intelligence route is unavailable through the real product path;
- user cannot inspect/cancel/pause the task;
- required high-impact approvals can be bypassed;
- release/package/provenance/security gates for the exact candidate are red.

V0.1 is **not** blocked solely because later-version Product Factory, Business Factory, Model Engineering Lab, AI Trader, advanced media/OCR/ASR, every model backend or universal computer-use coverage is incomplete.

# V0.2 — Personal Operations Beta

Goal: make the usable V0.1 team substantially more capable for daily personal operations without requiring autonomous software-product creation.

Expected scope:

- richer natural-language task/team creation and reusable templates;
- stronger long-term task/workspace memory and user-approved knowledge reuse;
- richer calendars/schedules/monitoring rules and notifications;
- broader browser and Windows semantic interaction coverage;
- Universal Research + Corpus integration for durable evidence/search workflows;
- stronger multi-agent collaboration, evaluation and recovery;
- more local/provider intelligence options after their own gates;
- stable forward migration from V0.1 user data.

# V0.3 — Capability Builder Beta

Goal: allow Nika to safely extend a running operational workflow when it lacks a narrow capability.

Expected scope:

- Agent Builder/workspace creation from natural language through real final UI;
- Toolsmith capability-gap lifecycle;
- reuse search before build;
- isolated Software Factory/CodingWorker implementation for narrow capabilities;
- independent test/security/compatibility review before registration;
- resume the original durable task after a capability is safely registered;
- broader media/OCR/speech specialists where already qualified.

# V0.4 — Autonomous Product Factory Beta

Goal: turn a complete digital-product request into a durable ProductProject lifecycle.

Expected scope:

- Research -> requirements -> decisions -> architecture;
- dynamic specialist team composition;
- one or multiple repositories/components;
- isolated implementation;
- trusted producer/reviewer authority and independent QA/accessibility review;
- checkpoint/restart authority;
- build/package/release provenance;
- approved staging/deployment, health and rollback where applicable;
- maintenance/repair lifecycle.

This release is where the representative Autonomous Product Factory gate becomes a declared product capability rather than a future/full-product requirement.

# V0.5 — Expanded Intelligence and Labs Beta

Goal: converge the major optional intelligence/research/lab capabilities that do not need to block V0.1–V0.4.

Expected scope as individually qualified:

- Model Engineering Lab and resource-aware model benchmarking/promotion;
- expanded local embedded intelligence including hardware-proven routes;
- advanced Research/Corpus/Media Intelligence;
- OCR/ASR/document/media optional components;
- AI Trader research/paper-trading workspace;
- broader Accessibility Repair/Computer Interaction capabilities;
- Business Factory controlled beta where policy and product dependencies are ready.

# V1.0 — Nika Core Stable

V1.0 is the first release that may claim the selected stable Full Product Vision scope on one exact integrated head.

It requires:

- all capabilities claimed for V1.0 integrated on one exact head;
- full dependency/security/provenance/governance/recovery gates;
- fresh Windows package and release evidence;
- complete declared Product Journeys;
- representative Product Factory acceptance if Product Factory is claimed stable;
- migration/update/rollback evidence;
- human Windows acceptance;
- human NVDA verification for the exact packaged candidate.

# Parallel priority model

Development therefore operates with two simultaneous truths:

## Release train

Seal the nearest user-usable version, with **V0.1 as the current highest product-priority target** until its declared acceptance is complete.

## Full-product development

Continue independent future-version work in parallel so V0.2+ and V1.0 do not stall while V0.1 is being integrated, audited, packaged and human-tested.

When a worker has a choice among equally safe, non-colliding tasks, prefer work that closes a current V0.1 blocker. Do not abandon an already-owned critical full-product repair simply because it belongs to a later release; finish coherent work or hand it off safely.

# Evidence reporting

Every cycle report should now state, where relevant:

- which staged release(s) the work advances;
- whether it closes a declared V0.1 blocker;
- exact integration/green evidence;
- what remains unverified for V0.1;
- whether the work is future-version-only and therefore non-blocking for the nearest user release.

Until a packaged candidate is actually human tested:

- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- do not describe V0.1 as released merely because backend or CI tests are green.
