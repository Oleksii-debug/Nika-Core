# Nika Core — Professional Autonomy and Upper-Bound Workloads

Status: architecture clarification candidate for review.
Date: 2026-09-01.
Applies to: V0.1 active release and future Full Product Vision.

## 1. Product positioning

Nika is not intended to be a brittle macro recorder, a fixed selector script, a wrapper around one LLM, or a toy coding assistant. Its product value comes from combining deterministic durable execution, replaceable intelligence, semantic computer interaction, controlled capability growth, hierarchical teams, long-lived ProductProjects, verification and recovery.

The correct comparison is not “can Nika do something a free chatbot can answer once?” The relevant question is whether Nika can own and continue a long-running operational or engineering program across tools, files, websites, repositories, workers, restarts and failures while preserving policy, evidence and state.

## 2. Adaptive computer interaction — not hard-coded button training

### 2.1 Required perception/action stack

For browser and Windows work, Nika uses a layered controller:

1. native/application API when available;
2. DOM/accessibility/UI Automation semantics;
3. deterministic action against named semantic controls;
4. adaptive semantic reasoning over the current interface state;
5. screenshot/OCR/vision grounding when semantics are incomplete;
6. coordinates only as a last-resort bounded fallback.

The agent must not depend on a single remembered CSS selector or screen coordinate when a semantic description can be reconstructed from the live page/application.

### 2.2 Adaptive semantic recovery

When a site/application changes, Nika should be able to:

- rebuild a fresh semantic map from roles, accessible names, labels, headings, relations, forms, document/frame identity and visible text;
- compare the current state with the declared task intent;
- propose a new semantic target when the old target disappeared or moved;
- reject ambiguity instead of clicking the first plausible control;
- verify the result after acting;
- record successful adaptations as versioned candidate skills/helpers only after deterministic replay/evaluation.

An LLM or vision model may propose “this appears to be the new Submit control,” but that proposal is not authority. ToolExecutor/permissions, target identity, action-risk classification and post-action evidence remain deterministic Nika contracts.

### 2.3 Novel or hostile UI

V0.1 must be adaptive within accessible/semantically recoverable interfaces, but it must not pretend that arbitrary hostile or anti-automation UI is always solvable. CAPTCHA, security challenges, intentionally hidden controls, inaccessible proprietary canvases, ambiguous destructive actions or platform restrictions may require a safe stop, user clarification or a later versioned adapter.

The required failure mode is “I cannot prove this is the intended action,” not unsafe guessing.

## 3. V0.1 team model

The V0.1 acceptance fixture uses exactly three representative agents because that is a tractable minimum proof of real multi-agent execution. It is not an architectural maximum.

### 3.1 Default mode

A user may choose “Automatic team.” Nika drafts an efficient team for the task. For the release acceptance scenario the default is supervisor + worker + checker/second worker.

### 3.2 Advanced bounded mode

V0.1 should expose, where current M6/M7 contracts permit without destabilizing the release, a bounded advanced team configuration:

- add/remove a worker role;
- assign role/purpose;
- choose allowed tools/workspaces/model route;
- set per-worker concurrency/resource limits;
- define success/verification responsibility;
- prevent child permissions from exceeding the task/project ceiling.

Therefore the user should be able to request three additional workers if resource and policy quotas allow it. The release gate still proves three; the runtime must not hard-code “three agents globally.”

Full dynamic team composition for large ProductProjects remains a later Product Factory capability.

## 4. V0.1 configuration UX — no thousand-field setup requirement

V0.1 should support three complementary configuration surfaces over one versioned schema.

### 4.1 Conversational Setup Wizard — default

The user describes the task naturally. Nika drafts a Task Profile and asks only for unresolved material choices, for example:

- what sources/targets are in scope;
- desired outcome and success evidence;
- schedule/deadline;
- what may be posted/sent/changed;
- team/model preference;
- budget/resource ceiling;
- output/report destination;
- what to do after network loss/restart;
- approval requirements.

The AI may draft configuration, but a deterministic validator owns the final schema and permission meaning.

### 4.2 Advanced structured editor

Power users can inspect/edit the complete profile using accessible grouped controls. The UI should expose only relevant fields by default and allow advanced sections to expand on demand.

### 4.3 Portable Nika Task Profile

Introduce a versioned portable artifact such as `.nika-task.json` or a packaged profile bundle. It may contain:

- profile/schema version;
- goal and success criteria;
- team/role declarations;
- model route policy;
- sources/targets;
- schedule/deadline/recurrence;
- browser/Windows action policy;
- retry/backoff/uncertain-effect policy;
- continuity/autostart behavior;
- outputs/reports;
- resource budgets;
- permission/approval references.

It must not contain raw persistent passwords, cookies, tokens or browser profiles. Credential references remain opaque.

A chatbot/consultant may help the user build this profile outside Nika, after which Nika validates/imports it. The same schema is also generated by the built-in conversational wizard.

## 5. V0.1 professional upper-bound scenario

A credible upper-bound V0.1 scenario is not “open one page and click one button.” It is a multi-day operational workflow such as:

“Monitor 300 declared public/vendor pages and a local document directory for three days. Use a three-to-bounded-small team. Every 30 minutes classify changed sources, download newly linked PDF/DOCX/XLSX/CSV/HTML files through supported extractors, normalize metadata, compare against previous observations, open semantically accessible browser workflows when API/HTTP is insufficient, fill declared forms or editors, verify resulting state, produce an auditable per-target report, pause automatically when network is unavailable, resume after reconnect/hibernate/reboot, never resend an uncertain external action, and stop at the deadline. High-impact posting/sending remains inside explicit approval/standing-policy scope.”

Technically this stresses Scheduler, durable cursor/checkpoints, MultiAgent runtime, ModelGateway, browser semantics, document parsers, ToolExecutor/approval/effect identity, retry/reconciliation, reports, Windows UX and recovery.

### V0.1 examples of technically possible work

Subject to site/app semantics, policy and required approval, V0.1 should be able to:

- navigate semantically accessible websites and supported Windows applications;
- search/read pages and documents;
- use ordinary text editors/web editors through semantic controls;
- enter structured text/forms;
- select options and invoke controls;
- monitor and compare changing pages;
- download/process supported files;
- coordinate a bounded multi-agent task;
- write reports/artifacts;
- perform declared external side effects only when current permission/approval allows and result verification/reconciliation is available.

A workflow such as “find a specific public Facebook post, draft a polite comment and place it in the comment editor” is technically within the computer-interaction model if the interface is semantically accessible. Actually posting the comment is an external side effect and must obey platform rules and the user’s declared approval/standing policy. Nika must not bypass CAPTCHA, account restrictions, anti-abuse controls or platform terms.

## 6. V0.2 professional upper-bound — autonomous operations and intelligence

V0.2 should be judged against a weeks-long operations/research program, not a single search.

Representative upper-bound scenario:

“Operate a multilingual competitive/regulatory intelligence program across 2,000+ declared sources, vendor portals, public databases, local document sets and scheduled browser-only sources. Maintain source health, incremental crawl state, document/version provenance, a local searchable corpus, change detection, entity/opportunity records, analyst-review queues, recurring reports and alerts. Use deterministic pre-filtering before expensive models, choose local/API intelligence according to privacy/cost policy, recover from source outages/reboots, and publish daily/weekly DOCX/XLSX/CSV/TXT/HTML deliverables without reprocessing unchanged material.”

V0.2 value is persistent operational intelligence: source management, corpus, incremental state, richer schedules, broader interaction coverage, report production and reusable workflows.

## 7. V0.3 professional upper-bound — capability acquisition and continuous improvement

V0.3 Toolsmith is not for trivial “read XYZ” examples. The professional requirement is that an ongoing task can encounter a missing operational capability and safely extend Nika without abandoning the program.

Representative scenario:

“A six-week compliance/data-conversion program receives data from twenty vendors. During execution Nika encounters an undocumented but legally accessible REST variant, a proprietary-but-documented export structure, a new enterprise SaaS workflow and a document format not covered by existing adapters. Nika must classify the capability gaps; search maintained SDKs/tools first; generate/adapt narrowly scoped connectors or parsers in isolated workspaces; construct deterministic fixtures from authorized data; run security/compatibility/accessibility tests; independently evaluate candidates; register successful versioned capabilities; resume the original workflow from checkpoint; and preserve rollback to the prior capability version.”

### 7.1 Continuous learning loop

Nika’s learning loop should be effectively unbounded in duration but bounded and evidence-driven per promotion:

Observe production/task episodes -> collect approved telemetry/evidence -> cluster repeated failures/opportunities -> propose challenger prompt/strategy/skill/tool/model configuration -> replay in sandbox/simulation/held-out datasets -> compare explicit metrics -> independent review where required -> promote versioned winner -> monitor regression -> rollback if worse -> repeat.

What may improve autonomously:

- prompts and routing strategies;
- deterministic heuristics;
- source ranking/dedup rules;
- browser semantic helpers;
- workflow policies inside declared bounds;
- model/provider selection policies;
- new isolated tools/adapters/plugins;
- ProductProject team/work decomposition strategies.

What may not silently self-promote:

- broader permissions;
- raw credential access;
- destructive/financial authority;
- production source that did not pass ordinary isolated implementation/review/release gates;
- unmeasured model/strategy changes.

## 8. V0.4 professional upper-bound — large software ProductProjects

V0.4 Product Factory is intended to manage product classes such as a social network, a secure messenger, an AI-enhanced mail client, an accessibility platform, or another large multi-component system. The promise is lifecycle management, not instant generation.

### 8.1 Example: social-network-class ProductProject

A request may specify functionality comparable in breadth to a mature social network without copying proprietary implementation/assets. Product Factory should be able to create a durable program with, for example:

- product/requirements/architecture governance;
- identity/auth/profile/privacy systems;
- social graph/follow/friend concepts;
- posts/comments/reactions/media;
- feeds/search/notifications/moderation/admin;
- web and mobile clients;
- backend APIs, storage/cache/search/media infrastructure;
- abuse/security/privacy/compliance workstreams;
- accessibility across web/mobile;
- analytics/observability;
- test/performance/security suites;
- CI/CD, staging, rollout, rollback and operations.

This is a months-long multi-repository ProductProject requiring many workers and remote/cloud execution. Nika must manage the program, not pretend one local model can write “Facebook” in one turn.

### 8.2 Example: messenger-class ProductProject

For a messenger combining strong ideas from Telegram/WhatsApp/Viber-class products, Nika may research public features, user pain points, standards and documented APIs; propose an independent architecture; and coordinate client/backend/security/accessibility/release work. It must not copy proprietary source/assets or evade service restrictions.

### 8.3 Example: Thunderbird-class AI mail client

A ProductProject may include desktop UI, account/provider adapters, local mail/index store, MIME/attachments, search, accessibility, AI summarization/drafting/classification, credential isolation, sync/recovery, packaging, update and migration.

## 9. Large AI/model engineering projects

A request to continue an AI project or train a 100M-parameter model is a legitimate ProductProject/Model Engineering Lab class when the user supplies lawful data rights, compute, budget and acceptance metrics.

Nika should be able to orchestrate:

- dataset ingestion/versioning/dedup/licensing/provenance;
- tokenizer/model/config experiments;
- training/evaluation pipelines;
- local or remote GPU execution nodes;
- checkpointing/resume;
- experiment tracking;
- held-out evaluation and safety/domain benchmarks;
- model artifact/version/license/checksum registry;
- packaging/serving adapters;
- regression and promotion/rollback.

Nika cannot manufacture missing compute, high-quality data or legal rights. Project feasibility is resource- and evidence-dependent.

## 10. Hierarchical engineering organization

Large ProductProjects require an explicit hierarchy rather than a flat swarm.

Canonical structure should support:

User/Product Authority
-> ProductProject Director/Coordinator
-> program/workstream leads (Product, Architecture, Security, Data/ML, Backend, Web, Windows, Mobile, QA, Accessibility, DevOps/Release, Operations)
-> component/team leads
-> implementation workers
-> independent reviewers/auditors
-> release/integration authority.

Required organizational properties:

- stable role and responsibility IDs;
- explicit component/repository ownership;
- dependency DAG;
- work queues and capacity/resource budgets;
- escalation paths;
- review separation;
- no worker silently overwriting another owner’s scope;
- shared-contract change control;
- handoff artifacts rather than reliance on chat memory;
- ability to add/remove/reassign specialists while preserving project state;
- measurable throughput/quality signals per workstream.

## 11. Coding-worker channels, including Codex/ChatGPT-class systems

Nika’s architecture should treat external coding/intelligence systems as replaceable workers behind Nika-owned ports.

Preferred integration order:

1. supported API/SDK;
2. supported CLI/desktop automation interface;
3. repository/task connector;
4. governed semantic browser interaction only when explicitly permitted and when no supported machine interface satisfies the requirement.

Codex, OpenHands or future coding systems can therefore be worker providers while Nika owns task decomposition, prompts/specs, repository/branch ownership, tests, evidence, retries, cost budgets and integration.

Using a consumer web UI specifically to bypass API charges, plan limits, rate limits or platform restrictions is not an architectural requirement and must not be implemented as an evasion mechanism. A semantic web worker may operate an authorized user-facing service only within that service’s terms and normal account controls.

For supported Codex usage, Nika should prefer official Codex clients/integrations (CLI/IDE/desktop/web interfaces where automation is supported) or supported APIs/connectors rather than brittle DOM driving.

## 12. Professional value proposition

The defensible value of Nika is not a claim that it outsmarts frontier models locally. It is the durable operating system around intelligence:

- owns project/task state for days to months;
- decomposes work and manages teams;
- uses multiple replaceable brains and workers;
- controls tools/computer interaction;
- survives failures and restarts;
- acquires capabilities under gates;
- verifies results independently;
- preserves provenance/audit/permissions;
- builds/packages/deploys/maintains products;
- learns from evidence without unsafe self-modification.

A high commercial valuation would depend on actually proving these properties on difficult real workloads; architecture documents alone do not justify a monetary price.

## 13. Acceptance extensions required by this clarification

Future acceptance work should add representative gates for:

1. adaptive semantic recovery after a controlled UI redesign without a hard-coded selector update;
2. ambiguous redesigned UI fails closed;
3. conversational wizard -> versioned Task Profile -> deterministic validation -> execution equivalence;
4. portable Task Profile import/export without raw secrets;
5. V0.1 bounded extra-worker configuration while the canonical three-agent acceptance remains valid;
6. multi-week V0.2 incremental operations simulation with unchanged-source cost avoidance;
7. V0.3 capability-gap build/register/resume with later rollback and repeated learning-cycle regression test;
8. hierarchical ProductProject with multiple workstreams/repositories and independent review ownership;
9. external coding-worker provider replacement without changing ProductProject domain contracts;
10. large AI/model project scheduling across local/remote execution nodes with restartable checkpoints.

These extensions must reuse existing Nika contracts and must not weaken the active V0.1 release gates.