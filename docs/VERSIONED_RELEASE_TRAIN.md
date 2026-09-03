# Nika Core — versioned release train

Status: binding release-sequencing and current execution-focus clarification for the existing Full Product Vision.

This document does **not** reduce the final Nika Core scope. `docs/MASTER_SPEC.md`, `docs/FULL_PRODUCT_VISION_2026-08-19.md`, `docs/ROADMAP.md`, Product Factory/Business Factory specifications, security boundaries, accessibility gates and release/provenance requirements remain binding.

The purpose of this release train is to create explicit intermediate finishes so the user receives a practical Windows Nika before the entire Full Product Vision is complete.

# Current execution mode — V0.1 ONLY until V0.1 release

The previous policy that allowed ordinary future-version development to continue in parallel is superseded for the current execution period.

Until V0.1 is accepted/released:

1. **All permanent-worker development, research, QA, release and integration effort is directed to V0.1 or to a proven direct V0.1 dependency.**
2. Do not start a new V0.2/V0.3/V0.4/V0.5/V1.0-only source, research, QA or integration lane.
3. Existing future-version PRs, branches and evidence are preserved; do not delete useful work merely because it is temporarily outside the nearest release.
4. A worker already inside a non-V0.1 mutation may finish only the smallest atomic safe step needed to leave the branch consistent and publish a durable handoff. It then freezes that future-version lane and retasks to V0.1.
5. Existing future-version code may be **REUSED** when it directly closes a V0.1 blocker. Reuse does not justify continuing unrelated later-version scope.
6. Every new production CLAIM must identify `V01_BLOCKER` and `V01_USER_JOURNEY`. A claim without a concrete V0.1 contribution should not begin.
7. P10-09 audits V0.1 candidates and direct V0.1 dependencies. P10-10 integrates V0.1 candidates/dependencies only, except the minimum safe closure of an already-running atomic integration operation.
8. Security, accessibility, restart safety, provenance and exact-head evidence are not weakened to make V0.1 faster.
9. `HUMAN_TESTED` and `NVDA_VERIFIED` remain human-only states.

# V0.1 — Usable Agent Team Alpha

## User outcome

V0.1 is the first release the user can use as a practical Windows digital-worker application rather than as a backend demonstration.

The defining capability is a **real team of three agents** that can execute long-running timed operational workflows using local or API intelligence and interact with websites and supported Windows interfaces under Nika policy.

The first version must be able to perform the same **class of workflow** as a practical browser automation script: process many declared targets in batches, open tabs, wait for controls/state, enter information, invoke an allowed action, verify the result, handle temporary busy/error states, wait between batches, pause/resume, preserve progress and produce a report. It must do this through Nika-owned durable/security/interaction contracts rather than as one hard-coded site script.

V0.1 is **not** required to complete Autonomous Product Factory, Business Factory, AI Trader, Model Engineering Lab, every media engine, every local-model backend or autonomous software creation.

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

The exact internal roles may vary by task, but V0.1 must prove a representative supervisor/worker/checker pattern:

- agent A coordinates/decomposes the goal and observes team state;
- agent B performs one independent operational/research/interaction slice;
- agent C performs another slice, monitoring/checking/comparison or verification work;
- agents exchange typed handoffs/results through Nika-owned contracts;
- independent subtasks may run concurrently within configured quotas;
- failure/cancellation of one worker does not corrupt the team or unrelated work;
- child agents never exceed the parent/team permission ceiling.

Three is the minimum proven V0.1 representative team journey, not a permanent global agent-count limit.

### 3. Local or API AI through ModelGateway

V0.1 must support at least one practical local route and one practical API/cloud-compatible route through the provider-neutral ModelGateway contract when configured by the user.

Preferred V0.1 unblockers are existing supported external-local/OpenAI-compatible routes such as Ollama and approved API providers. Foundry Local physical-hardware proof is valuable but does not block V0.1 when another already-supported local route satisfies the declared journey.

Provider secrets remain outside prompts, ordinary logs and durable task state.

### 4. Durable timing, waiting and recurring execution

The user can express workflows such as:

- start at a specified clock time;
- wait until a specified time before the next step;
- wait/delay for a bounded duration, then continue;
- repeat every declared interval;
- run on a recurring schedule;
- monitor during a declared time window;
- stop at a deadline or when the success condition is met.

Implementation reuses SchedulerPort/APScheduler plus durable runtime/checkpoint semantics. Long waits must not depend on a busy loop or one fragile process-local `sleep`.

Required behavior:

- schedule identity and next intended action survive app restart for durable tasks;
- restart reconstructs the workflow without duplicating completed external effects;
- missed-run behavior is explicit: run-late, skip or fail/block according to policy;
- pause/cancel prevents future scheduled effects until resumed;
- retry/idempotency rules remain authoritative for external actions.

### 5. Website monitoring, search and state comparison

A three-agent team can perform a durable monitoring/search task over user-declared web sources:

- open or revisit declared pages;
- extract relevant semantic content;
- search/filter/compare information;
- detect meaningful change or a declared condition;
- record evidence/result state;
- continue according to schedule;
- present an accessible user-visible report or alert state.

Monitoring should avoid unnecessarily reprocessing unchanged content where existing research/change-state capability can be reused.

### 6. Browser tabs and semantic actions

V0.1 must prove a real browser interaction path based on Playwright/semantic browser control before screenshot or coordinate fallbacks.

Representative actions include:

- open a page or new tab;
- switch among task-owned tabs/pages;
- navigate by URL or semantic link;
- locate controls by role/label/name/visible semantic text;
- click a declared button/link;
- enter text in a declared field;
- select a declared option where supported;
- wait for a semantic condition, navigation, content change or bounded timeout;
- read resulting page state and return evidence;
- close task-owned tabs when appropriate.

The agent must not silently use arbitrary coordinates when semantic DOM/accessibility information exists.

### 7. Script-class batch workflow runner — mandatory V0.1 acceptance family

The supplied Batch Queue Runner is a **behavioral reference**, not code to copy and not a ChatGPT-specific product requirement. V0.1 must generalize its useful control pattern.

A V0.1 workflow may receive a declared list of URLs/targets plus a user-approved action description and must be able to:

1. divide targets into bounded batches, for example 20 targets processed five at a time;
2. persist the batch cursor and intended next work **before** spawning the external tab/action where needed to prevent crash/reload duplication;
3. open each target in a task-owned browser tab;
4. wait for the page and the named/semantic control to become usable;
5. detect temporary busy/loading/disabled conditions rather than blindly clicking;
6. enter declared content into a semantic input/editor;
7. invoke the allowed action under the applicable standing permission or explicit approval boundary;
8. verify observable success/effect before marking the target done;
9. classify and record failure reasons;
10. use bounded retry/backoff for transient busy/rate-limit/network states rather than infinite loops;
11. wait a user-declared interval between batches without holding a fragile foreground sleep;
12. support global pause/resume/cancel;
13. survive Nika close/reopen and resume from durable progress without repeating already-confirmed external effects;
14. produce an accessible per-target report containing at least target identity, opened/attempted state, success/failure, reason/evidence, time and relevant result;
15. keep target tabs/actions scoped to the task and close them when policy/workflow says they are no longer needed.

This capability must be generic. Do not hard-code ChatGPT selectors, a single site URL or one special prompt. Site/app-specific profiles may exist as narrow adapters, but the orchestration contract is Nika-owned and semantic-first.

### 8. Supported Windows semantic interaction

V0.1 exposes the already-supported Windows interaction boundary for bounded real tasks where current integrated evidence permits it.

Priority:

1. native/application API;
2. Windows UI Automation/accessibility semantics;
3. deterministic named controls;
4. OCR/vision fallback;
5. coordinates only as a last-resort bounded fallback.

V0.1 does not promise universal control of every Windows application. Unsupported targets fail clearly rather than pretending success.

### 9. Permissions, approvals and visible audit

Low-risk observation/navigation may proceed under configured policy. Higher-impact actions remain governed by Nika approval boundaries.

V0.1 must not turn "agent can click" into unrestricted computer control. Send/delete/publish/account/financial/code-execution or other high-impact actions remain previewed/audited/approved according to existing policy.

Standing permissions may reduce repetitive prompts only inside a bounded user-declared scope and never silently widen themselves.

### 10. Restart/resume and side-effect safety

A representative long-running timed three-agent workflow must survive:

- ordinary application close/reopen;
- restart while waiting for a future scheduled step;
- restart after at least one completed batch/step;
- worker failure where unrelated team state remains valid.

Completed external side effects must not be repeated merely because Nika restarted. Uncertain effects must reconcile/fail safely according to the existing runtime side-effect policy rather than being blindly retried.

## Representative V0.1 acceptance scenario A — long monitoring team

From the packaged Windows UI, configure local or API intelligence and issue a controlled task equivalent to:

> Use a team of three agents. At a user-specified start time, open two declared web sources in task-owned tabs. Let two workers inspect/search them independently and let the third compare/check the results. If a declared condition is absent, wait until the next scheduled interval and check again. Continue until the deadline or condition is met. Show current team state and accumulated result. I must be able to pause the task, close Nika, reopen it while it is waiting, and resume without repeating a previously completed external action.

## Representative V0.1 acceptance scenario B — script-class batch workflow

Use controlled/local test pages in automated CI so the test is deterministic. The user supplies a workflow equivalent to:

> Process 20 declared target pages five at a time. Open each target in its own task-owned tab. Wait for a named input and named action control. Enter the declared test command, invoke the permitted action, verify the success state and record the result. If a target is temporarily busy, retry within a bounded policy. After each batch, wait 60 seconds before the next batch. Allow me to pause after batch 2. If Nika is closed while paused/waiting, reopen it and resume at the correct next target without repeating successful actions. At the end, show an accessible report for every target.

The automated fixture must not depend on a real ChatGPT account or another mutable external service. A separate user-controlled/manual scenario may exercise a real external site only within that site's terms and Nika permission policy.

## V0.1 release blockers

V0.1 is blocked by defects in capabilities it claims, including:

- packaged UI not wired to real task/team/runtime state;
- no real three-agent team execution through final UI;
- timing/waiting implemented only as fragile process-local sleeps;
- schedule/restart loses or duplicates work;
- no generic batch-target cursor/progress model for the script-class scenario;
- browser actions are mock-only or coordinate-only when semantics exist;
- action completion is marked without observable verification;
- transient errors can create infinite retry loops or duplicate effects;
- local/API intelligence route is unavailable through the real product path;
- user cannot inspect/pause/cancel/resume the task;
- required high-impact approvals can be bypassed;
- exact release/package/provenance/security gates are red;
- packaged Windows UI cannot expose the workflow accessibly enough for the human/NVDA acceptance protocol.

# V0.1 worker allocation until release

All ten permanent roles remain active, but their work is retasked to one release rather than later-version feature expansion.

- **P10-01 — integration/control plane:** maintain the V0.1 blocker DAG, ownership, acceptance matrix and sequencing. Reject new future-version-only claims.
- **P10-02 — team/task product path:** converge the real three-agent operational journey and only ProductProject/Product Factory pieces that are direct dependencies of that journey. Do not advance full Product Factory scope merely for V0.4.
- **P10-03 — tools/adapters:** close only missing tools/adapters needed by the V0.1 workflow; do not expand general Software Factory/Toolsmith roadmap for later versions.
- **P10-04 — security/approval:** prove bounded browser/Windows standing permissions, approval boundaries, credential handling, effect authority and audit for V0.1 actions.
- **P10-05 — runtime/durability:** own durable timing/waiting, restart/recovery, pause/cancel, idempotency/reconciliation and non-duplicate side effects for V0.1.
- **P10-06 — monitoring/search:** concentrate Research on the monitoring/search/change-detection/report functions required by V0.1; freeze Media/Trader/Labs work unless a specific V0.1 dependency is proven.
- **P10-07 — Windows/UIA/Product Journey:** wire the packaged Windows UI, three-agent/task controls, browser/Windows semantic interaction, keyboard accessibility and pre-human Product Journey for V0.1.
- **P10-08 — package/release:** produce exact-SHA V0.1 packaging, provenance, secrets scan, notices/SBOM as applicable, update/backup/recovery and repository-governance evidence required for the candidate.
- **P10-09 — independent audit:** spend QA capacity on V0.1 production candidates/direct dependencies and the two representative V0.1 end-to-end scenarios; avoid new future-version-only QA oracles.
- **P10-10 — integration/sealing:** merge only exact-current V0.1 dependencies/candidates with required audit/green evidence, rebuild the V0.1 DAG after each integration, and seal the candidate.

## Claim/report rule during V0.1 focus

Every new CLAIM should state:

- `V01_BLOCKER=<specific blocker>`;
- `V01_USER_JOURNEY=<UI/team/timing/browser/monitoring/security/restart/package/etc.>`;
- exact owned slice/branch;
- why the work is required for V0.1 now.

Every cycle report should state whether the blocker is integrated, still candidate-only, waiting audit/CI, or not yet proven.

# Later versions — planned, currently frozen for new work

The following scopes remain part of the product plan but do not receive new future-version-only execution until V0.1 is released.

## V0.2 — Personal Operations Beta

Richer memory, schedules, monitoring, browser/Windows coverage, Research/Corpus integration, stronger collaboration and more qualified AI routes.

## V0.3 — Capability Builder Beta

Agent Builder, Toolsmith capability-gap lifecycle, reuse-before-build, isolated Software Factory/CodingWorker and safe registration/resume.

## V0.4 — Autonomous Product Factory Beta

Full ProductProject lifecycle: research, requirements, dynamic teams, one/multiple repos, implementation, independent QA/accessibility, checkpoint/restart, build/package/release, deployment/rollback and maintenance.

## V0.5 — Expanded Intelligence and Labs Beta

Model Engineering Lab, broader embedded/local intelligence, advanced Research/Corpus/Media, OCR/ASR, AI Trader research/paper mode, broader Accessibility Repair/Computer Interaction and controlled Business Factory beta.

## V1.0 — Nika Core Stable

Selected stable Full Product Vision on one exact integrated head with complete declared Product Journeys, security/provenance/governance/recovery/update/rollback evidence, fresh Windows package, human Windows acceptance and human NVDA verification.

# Release truth

Until a packaged candidate is actually tested by a human:

- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- do not describe V0.1 as released merely because backend or CI tests are green.
