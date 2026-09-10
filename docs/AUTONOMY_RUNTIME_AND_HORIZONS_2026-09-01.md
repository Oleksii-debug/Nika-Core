# Nika Core — Autonomy Runtime, Self-Improvement and Execution Horizons

Status: architecture clarification candidate for review; documentation-only.
Date: 2026-09-01.
Base policy: V0.1 remains the only active release target until sealed. Future horizons described here are not V0.1 implementation credit.

This document extends `PROFESSIONAL_AUTONOMY_UPPER_BOUND_2026-09-01.md` with the autonomy requirements clarified by the user on 2026-09-01. It does not weaken safety, accessibility, approval, exact-effect, restart, packaging or release gates.

## 1. Product principle — autonomy, not brittle automation

Nika must not be designed as a collection of brittle scripts that only work while a site or application looks exactly as it did when the script was authored.

The target model is:

`goal -> observe -> diagnose -> understand -> plan -> validate -> act -> verify -> record experience -> adapt -> continue`

A stored selector, coordinate, prompt or workflow may be a useful hint, but it is never sufficient authority when the current environment no longer matches its assumptions.

Nika must distinguish:
- deterministic known workflow execution;
- adaptive semantic recovery after ordinary UI change;
- diagnostic reconnaissance when the environment is unclear;
- vision/OCR fallback when semantics are insufficient;
- capability acquisition when a missing reusable tool or adapter is proven;
- fail-closed escalation when correctness, authority or safety cannot be established.

## 2. Site Reconnaissance and Diagnostic Probe Plane

### 2.1 Objective

For complex or changed websites, Nika must be able to inspect the live application before acting rather than relying only on previously learned buttons or selectors.

The browser interaction stack should expose a controlled diagnostic plane behind Nika-owned contracts. Candidate implementation reuses Playwright and browser DevTools/CDP capabilities rather than creating a browser engine.

### 2.2 Diagnostic capabilities

Where permitted by the target and browser security model, read-only diagnostic probes may inspect:
- DOM structure and current document generation;
- accessibility tree, roles, names and labels;
- visible/hidden/disabled state;
- forms, inputs, contenteditable regions and validation attributes;
- shadow DOM boundaries where technically accessible;
- iframes and origin boundaries;
- route/navigation state and SPA transitions;
- page readiness and pending application state;
- event-listener metadata where browser tooling legitimately exposes it;
- client-side state exposed to the page and safe to inspect;
- network request/response metadata needed to understand application flow, with credential/header/body redaction policy;
- console errors and page exceptions, safely normalized;
- API endpoints visible through ordinary browser execution when their use is permitted;
- current control-to-form relationships and post-action observable effects.

`page.evaluate()` or equivalent injected JavaScript may be used for diagnostics and deterministic page-local operations when it is safer and more reliable than simulated clicking. CDP/DevTools instrumentation may be used where Playwright's ordinary semantic surface is insufficient.

### 2.3 JS/CDP is not a security bypass

Diagnostic or page-context scripting must never be used to:
- bypass authentication or authorization;
- defeat CAPTCHA or anti-bot/security challenges;
- disable security controls;
- exfiltrate cookies, passwords, tokens or unrelated user data;
- evade service rate limits or platform policy;
- execute an external side effect without the same Nika approval/effect authority required by semantic clicking.

The diagnostic plane is read-only by default. Any mutating page-context operation is a governed ToolExecutor action with explicit effect identity, risk and verification.

### 2.4 Adaptive site model

Nika should maintain a versioned, bounded Site Model for recurring environments:
- site/origin identity;
- known task-relevant semantic regions;
- known workflows and success signals;
- prior UI generations;
- observed failure modes;
- stable API/DOM/UIA opportunities;
- confidence and freshness;
- invalidation conditions.

A changed page triggers re-observation and, when needed, diagnostic reconnaissance. It does not silently reuse a stale selector as truth.

## 3. Discovery-first autonomy

Examples such as “20 declared targets” or “300 supplied sources” are acceptance fixtures or bounded work sets; they must not become a product requirement that the user manually enumerate the world for Nika.

The professional product must support goals such as:

`Research this domain across at least 10,000 relevant public sources, discover the sources yourself, rank them, remove duplicates, validate them, monitor useful ones and produce evidence-backed outputs.`

### 3.1 Source Discovery Pipeline

Required architecture:

`Research Goal -> Query/Seed Generation -> Search/Directory/API Discovery -> Candidate Source Graph -> Validation -> Quality/Relevance/Authority Scoring -> Deduplication -> Coverage Analysis -> Crawl/Monitor Queue -> Evidence Store -> Iterative Gap Discovery`

Nika should discover sources through maintained search/provider APIs, public directories, site links, sitemaps, known registries, user-approved connectors and research evidence. Browser search is fallback where direct APIs/connectors are unavailable and permitted.

### 3.2 Coverage is an optimization problem

For large research programs, Nika should track:
- language/country/domain coverage;
- source authority and freshness;
- duplication/near-duplication;
- source health;
- cost per useful evidence item;
- discovery saturation;
- unresolved evidence gaps;
- provenance and confidence.

The user specifies the objective, constraints and acceptable coverage level. Nika owns the internal source-discovery work unless a material decision is required.

### 3.3 Version placement

V0.1 proves the bounded execution mechanics with deterministic controlled fixtures and may include small automatic target discovery where it is a direct dependency.

V0.2 is the first release where autonomous large-scale discovery, iterative source expansion and corpus-level research are expected product capabilities.

## 4. Quiet Autonomy — management by exception

A professional autonomous system must not expose every internal capability gap, retry, adapter repair, failed experiment or implementation detail to the user.

The default user contract is:
- user provides goal, constraints, permissions, budget/resource policy and success criteria;
- Nika manages ordinary decomposition, retries, diagnostics, tool choice, internal repair and background improvement;
- user sees progress, meaningful outcomes and actionable exceptions;
- internal engineering detail remains available on demand in diagnostics/audit views.

### 4.1 Exceptions that must surface

Nika must interrupt or explicitly report when required by policy or product correctness, including:
- a new credential/account authorization is needed;
- money/spending/contract authority is required;
- a destructive or high-impact action requires approval;
- legal/licensing/privacy or identity decision requires the user;
- a deadline/SLA will materially change;
- an objective cannot be achieved under the current permission/resource ceiling;
- a safety boundary would otherwise be crossed;
- a persistent failure materially degrades the requested result.

Ordinary internal implementation difficulty is not, by itself, a reason to burden the user.

### 4.2 UI behavior

Default UI shows concise states such as:
- Working;
- Waiting for network;
- Recovering;
- Improving internal method;
- Waiting for required approval;
- Completed with result;
- Completed with declared limitations.

A separate expert diagnostics/audit view may expose the detailed internal graph, attempts, tests, adapters and evidence.

## 5. Experience Ledger

Every governed Nika task should be able to produce bounded, privacy-aware operational experience records.

The Experience Ledger is not raw surveillance or an unlimited log. It records task-relevant evidence such as:
- environment/capability identity and version;
- strategy/tool/adapter used;
- task class;
- success/failure category;
- latency/resource cost;
- retries and recovery path;
- UI/site generation change;
- ambiguity encountered;
- verification quality;
- failure reproduction fixture/reference;
- safe outcome metrics;
- whether a later strategy improved the result.

Raw secrets, credentials, unrelated personal content and unrestricted page dumps are excluded by default.

Experience is scoped by user/workspace/site/product privacy policy.

## 6. Idle Improvement Engine

### 6.1 Principle

If the user is away and resources are available, Nika should be able to spend otherwise-idle compute on useful internal work rather than remaining passive.

Examples:
- replaying previously failed deterministic fixtures;
- testing alternative semantic locators and diagnostic strategies;
- building synthetic regression cases from observed failures;
- benchmarking local model/provider routing;
- evaluating prompt/planning/team variants;
- validating connectors and tool health;
- refreshing local indexes;
- improving source ranking/dedup rules;
- generating candidate narrow adapters/helpers;
- performing dependency/security/license checks;
- running selected ProductProject QA/backlog work;
- precomputing offline summaries/indexes/embeddings when approved.

### 6.2 Resource modes

Resource Manager should support at least:
- INTERACTIVE — foreground latency wins, background improvement heavily restricted;
- BALANCED — spare CPU/RAM may be used conservatively;
- AWAY — user declared absent for a period; heavier background work allowed;
- OVERNIGHT_HEAVY — explicit schedule permits high local utilization;
- BATTERY_ECONOMY — suspend nonessential improvement work;
- CUSTOM — per-resource budgets and allowed job classes.

User presence may be inferred only through ordinary OS activity/resource signals under privacy policy, or declared explicitly (“I will be away for an hour”). No covert surveillance is required.

### 6.3 Preemption

Foreground/user work always has a configurable priority over background self-improvement. Background jobs must checkpoint and yield resources when:
- the user returns;
- foreground task latency rises;
- RAM/CPU/GPU/thermal/battery thresholds are exceeded;
- a more important durable task becomes runnable.

## 7. Offline autonomy

Loss of internet must not reduce Nika to a dead shell.

When cloud/network capabilities are unavailable, Nika may continue any permitted work supported by:
- Deterministic Brain;
- installed embedded/local model;
- local Ollama/OpenAI-compatible server;
- local files/corpus/search;
- local tests/replay/benchmarks;
- local coding sandboxes;
- local Windows applications;
- queued outbound work that can wait safely.

Cloud-only operations become durable WAITING/OFFLINE jobs. They resume after connectivity returns and authority/readiness are revalidated.

The Idle Improvement Engine may preferentially perform offline-capable work during outages.

## 8. Controlled Self-Improvement Loop

Nika may continuously improve operational competence, but no production mutation is accepted merely because Nika proposed it.

Required loop:

`Experience -> Pattern/Failure Detection -> Improvement Hypothesis -> Candidate Strategy/Prompt/Tool/Adapter/Code -> Isolated Clone/Branch/Sandbox -> Deterministic Replay + Regression + Security/Compatibility/Accessibility Tests -> Independent Evaluation -> Versioned Promotion -> Runtime Monitoring -> Automatic/Approved Rollback if Worse`

### 8.1 What can improve automatically within policy

Candidate improvements may cover:
- prompts and structured instructions;
- task decomposition;
- source ranking and deduplication;
- model/provider selection;
- worker/team composition;
- semantic browser strategies;
- diagnostic probes;
- deterministic plans;
- retry/backoff strategy within safety contracts;
- report generation;
- narrow tools/adapters/plugins;
- coding/test strategies;
- resource scheduling.

### 8.2 Non-negotiable boundaries

Self-improvement may not silently:
- grant itself new permissions;
- read unrelated credentials;
- disable safety/audit/approval gates;
- deploy failed candidates;
- edit live production source in place;
- make an experimental copy authoritative without evidence;
- rewrite historical audit/evidence.

Nika may create a clone/branch/worktree of itself, modify it, run experiments and compare it against the current version. Promotion still uses the same protected release mechanism as any other production change.

## 9. Persistent Self-Model — technical metacognition, not a sentience claim

The project may pursue increasingly strong self-monitoring and metacognition. It must not claim literal consciousness or subjective sentience without evidence that software systems do not currently provide.

The useful engineering target is a persistent Self Model containing versioned facts such as:
- current capabilities and unavailable capabilities;
- known reliability by task/environment class;
- current tools/providers/models and health;
- resource state;
- current commitments/tasks/projects;
- confidence/uncertainty calibration;
- known recurring failure patterns;
- experience/improvement history;
- current software/release identity;
- active policy/permission ceilings;
- which strategies are proven, experimental or retired.

This lets Nika reason about itself operationally: “which method works best here?”, “what am I weak at?”, “what can I improve offline tonight?”, “is this capability degraded?”, and “does this candidate actually outperform my current behavior?”

### 9.1 Version placement

V0.1:
- capability/health inventory foundation;
- resource awareness;
- experience/evidence capture for V0.1 journeys;
- clear distinction between current/proven/uncertain behavior.

V0.2:
- reusable environment/site/workflow experience;
- operational learning from repeated research/interaction;
- idle replay/benchmark engine;
- resource-aware offline improvement jobs.

V0.3:
- controlled Toolsmith/self-improvement promotion loop;
- strategy/prompt/tool/adapter experiments;
- automatic regression comparison and rollback under policy.

V0.4+:
- self-improvement applied to Product Factory organization, coding, testing, deployment and maintenance strategies.

Post-V1 horizons may deepen persona continuity, autobiographical memory and cognitive self-modeling, but these remain software capabilities rather than unsupported claims of consciousness.

## 10. Integration and Interoperability Fabric

Nika should become easier to extend by adopting existing maintained ecosystems rather than recreating every connector.

Priority order:
1. native provider/application API or SDK;
2. standardized connector/protocol;
3. MCP tool/resource integration;
4. A2A-style agent interoperability where a remote specialist agent is appropriate;
5. Nika plugin/workspace adapter;
6. semantic UI interaction where no better interface exists;
7. vision/coordinates last.

### 10.1 Connector Registry

A provider-neutral registry should track:
- connector/agent identity;
- capabilities;
- version/protocol version;
- authentication method;
- permission/risk classes;
- health/last validation;
- data/privacy classification;
- rate/cost constraints;
- supported operations;
- fallback/replacement options.

### 10.2 MCP

Use the maintained MCP SDK/specification for compatible tool/resource interoperability rather than custom protocol framing. Nika retains permission/effect/audit authority.

### 10.3 Agent-to-Agent interoperability

Evaluate Google A2A or compatible open agent interoperability behind a Nika-owned port so Nika can discover/invoke remote specialist agents without importing their internal framework types into Nika domain contracts.

### 10.4 First-party connectors

High-value direct connectors may include GitHub, Google Drive/Docs/Sheets, Gmail/Calendar/Contacts, Microsoft 365, cloud/storage providers, databases and approved business systems. CredentialBroker/IdentityBroker owns credentials; models receive opaque references, not raw persistent secrets.

## 11. Professional Engineering Floor

“Million-dollar-grade” is used here as an engineering-quality metaphor, not a valuation claim.

A Nika capability intended for professional use should be evaluated against a high floor:
- durable operation over days/weeks/months;
- crash/reboot/network/sleep recovery;
- safe concurrency;
- exactly-once/reconciliation for external effects;
- observability and audit;
- accessibility;
- security/least privilege;
- credential isolation;
- deterministic rollback;
- versioned schemas/contracts;
- multi-provider replaceability;
- resource/cost governance;
- independent QA;
- package/deployment provenance;
- maintainability and self-diagnostics;
- scale tests appropriate to the claimed workload;
- user-facing simplicity despite internal complexity.

Toy demos do not satisfy this floor.

## 12. Competitive Architecture Benchmark

Nika should continuously benchmark its architecture against relevant external systems rather than assume uniqueness.

### Microsoft direction to match/exceed

Modern Copilot Studio emphasizes adaptive computer use across web/desktop, workflows, credentials/governance, observability and real-time voice.

Nika differentiation target:
- semantic + diagnostic JS/CDP + vision layered interaction;
- user-owned durable local state;
- offline deterministic/local operation;
- cross-reboot personal autonomy;
- self-improvement experiments;
- Windows/NVDA-first accessibility;
- Product Factory and local resource-aware workers in one control plane.

### Google direction to match/exceed

Google ADK/A2A emphasizes multi-agent composition, remote cross-language agents and interoperability; Google also provides computer-use and live multimodal model surfaces.

Nika differentiation target:
- consume interoperable remote agents rather than isolate itself;
- keep durable user-owned ProductProject/task truth outside any one provider;
- combine deterministic/local/cloud workers;
- manage exact effects, permissions, restart and long-lived operations centrally.

### OpenAI/Codex direction to match/exceed

Codex emphasizes parallel coding agents, worktrees/environments, background work and end-to-end engineering tasks.

Nika differentiation target:
- use Codex/OpenHands/other coding systems as replaceable execution workers;
- own cross-provider project hierarchy, acceptance, release, credentials, resource policy and maintenance;
- combine software work with browser/Windows/research/business/voice workflows rather than become only a coding command center.

### Protocol ecosystem

MCP and A2A reduce custom integration cost. Nika should integrate these rather than rebuild equivalent protocol stacks.

## 13. Voice and Persona Horizon — Nika Voice

Nika Voice is a future ProductProject/profile over Nika Core, not a requirement to delay V0.1.

Target user experience:
- user can say an opt-in wake phrase such as “Ніка” without pressing a button;
- local keyword spotting/VAD wakes the interaction path;
- user can speak from across the room when microphone hardware/acoustics permit;
- Nika responds with low-latency speech;
- barge-in allows the user to interrupt naturally;
- user can ask status, change priorities, add thoughts, start tasks or have an ordinary conversation;
- the same durable task/project context is used as the text command center;
- voice can use a user-selected expressive persona, including a young female voice if desired;
- persona may have persistent user-approved memory and relationship style, but remains a software persona.

### 13.1 Privacy-first wake architecture

Default architecture should favor:

`Microphone -> local wake-word detector -> local VAD -> local or approved STT -> Nika Command/Conversation Router -> ModelGateway/Deterministic path -> TTS`

Ambient audio should not be continuously streamed to a cloud provider just to detect the wake word. Wake listening is explicitly opt-in, visible and disableable.

Candidate reusable components include sherpa-onnx keyword spotting/ASR/TTS and measured Whisper/local alternatives. Cloud/live voice providers remain optional behind stable Nika ports.

### 13.2 Voice permissions

Voice identity is not automatically sufficient authorization for high-impact operations. Voice actions reuse the same R0-R4, approval, credential and audit boundaries as text/UI actions.

## 14. Nika Mobile Relay Horizon

A future Android companion can be intentionally thin rather than duplicating the entire Windows Nika.

Target:
- optional local wake word on Android subject to OS/background-microphone rules;
- secure pairing with a user's Nika installation/account;
- encrypted authenticated command/status transport;
- send voice/text notes, goals and priority changes while away;
- receive concise task/project status and important exception notifications;
- queue user notes when the home PC is unreachable and synchronize later;
- no direct remote exposure of the desktop SQLite DB or stored credentials;
- device revocation and audit.

The phone is a trusted relay/interface. Windows/home/server Nika remains the main durable control plane unless a future architecture explicitly promotes another execution node.

## 15. Industrial and Physical Execution Horizon

Industrial/physical execution is a post-V1/high-assurance horizon and must not be mixed into ordinary browser-agent acceptance.

Nika may eventually coordinate industrial engineering and robotic systems through governed adapters, but an LLM/agent is never a substitute for certified machine-safety control.

### 15.1 Reuse direction

Evaluate maintained standards/ecosystems such as:
- OPC UA for industrial semantic data/control and device/system information models;
- ROS 2 for robotics/middleware ecosystems;
- vendor APIs/SDKs;
- MQTT/Modbus or domain adapters where justified.

### 15.2 Safety architecture

Required separation:

`Nika goal/planning/supervision -> validated industrial intent -> digital twin/simulation -> approved execution adapter -> certified PLC/safety controller/interlock -> physical machine`

High-impact physical actions require R4-class policy as applicable plus hardware/industrial safeguards such as emergency stops, interlocks, geofencing, rate/force/speed limits, safe state and operator authority.

Nika may optimize plans and diagnose systems, but it cannot bypass safety PLCs, vendor protections or regulatory requirements.

## 16. Version / Horizon placement

### V0.1 — Durable Autonomous Operator Alpha
Active release target.

Must focus on:
- one real packaged Windows journey;
- adaptive semantic browser interaction foundation;
- bounded diagnostic site probing where directly needed by V0.1;
- real three-agent acceptance fixture with bounded optional extra workers architecture;
- browser batch/monitoring/local+API routes;
- network/restart/hibernate/reboot/autostart continuity;
- exact-effect safety;
- Experience Ledger foundation;
- capability/health/resource inventory foundation;
- quiet user-facing states;
- no Voice/Mobile/Industrial implementation required.

### V0.2 — Autonomous Research & Operations Platform
Expected expansions:
- large-scale source discovery without manual enumeration;
- thousands/10k+ source-class research programs with coverage/quality control;
- reusable site/environment models;
- incremental corpus and monitoring;
- resource-aware idle replay/benchmarking;
- deeper offline operational learning;
- broader connectors/integration fabric.

### V0.3 — Controlled Self-Improving Capability Platform
Expected expansions:
- full Toolsmith capability acquisition;
- candidate self-improvement in isolated clones/branches;
- experiment-driven prompt/strategy/tool/adapter promotion;
- automatic regression comparison and rollback;
- mature persistent Self Model and capability reliability map.

### V0.4 — Autonomous Product Factory
Expected expansions:
- hierarchical project organizations;
- multi-repository products;
- Codex/OpenHands/other replaceable coding workers;
- remote Windows/Linux/macOS/GPU execution nodes;
- independent QA/security/accessibility/release organizations;
- deployment/maintenance loops;
- product classes at social-network/messenger/AI-client/model-engineering scale, bounded by resources and time rather than toy architecture.

### V0.5 / V1.0 — Expanded Intelligence + Stable Integrated Platform
Expected expansions follow binding release train: advanced labs, model engineering, media/business capabilities and finally one stable integrated Windows platform with human/NVDA acceptance for the capabilities claimed by that release.

### Post-V1 optional products/horizons
- Nika Voice / Persona;
- Nika Mobile Relay;
- Industrial/Physical Execution Fabric;
- deeper multi-device/edge/robotic execution;
- richer cognitive self-model/persona research without unsupported sentience claims.

These horizons should reuse Core contracts rather than fork a second Nika.

## 17. Proposed acceptance extensions

These are proposed gates for integration into the canonical acceptance documents after review.

### Adaptive UI Recovery gate
Using controlled fixtures, mutate a known interface across multiple generations: rename controls, reorder layout, change DOM structure, introduce shadow/frame boundaries, add irrelevant controls and change readiness timing. Nika must re-observe/diagnose and complete semantically equivalent permitted tasks without stale-selector effects. Ambiguity/high-impact uncertainty fails closed.

### Site Diagnostic Safety gate
Read-only JS/CDP probes produce a bounded redacted Site Model. Synthetic cookie/token/header/password canaries must not appear in ordinary diagnostic artifacts. Diagnostic scripts cannot bypass ToolExecutor for mutating effects.

### Discovery-first Research gate
Given only a research goal and constraints, Nika discovers a controlled large source universe, validates/deduplicates/ranks sources, demonstrates coverage accounting and builds a bounded execution queue without requiring the user to enumerate the sources.

### Quiet Autonomy gate
Inject ordinary recoverable internal failures and capability maintenance events. User-facing flow remains concise and continues automatically. Inject approval/credential/legal/deadline/safety exceptions and prove they are surfaced rather than hidden.

### Experience/Improvement gate
A repeated controlled failure creates privacy-safe experience evidence. An idle improvement run proposes a challenger, evaluates it against fixed replay/held-out cases, promotes only if metrics improve, and rolls back on regression.

### Offline Improvement gate
Remove network connectivity. Nika continues permitted deterministic/local work and selected improvement jobs, persists cloud-dependent work as waiting, and resumes it after reconnect without duplicate external effects.

### Resource Preemption gate
Run background improvement under AWAY mode, then introduce foreground work/resource pressure. Background work checkpoints/yields and later resumes without corrupting its experiment state.

### Interoperability gate
Register at least one MCP tool/resource and one remote-agent-style test adapter behind Nika contracts. Capabilities are discoverable, permission-scoped, revocable and replaceable without leaking provider framework types into domain state.

### Voice privacy gate — future
Wake-word listening is opt-in/local by default; disabled means no listening path. Wake/STT/TTS states are visible. Voice commands obey normal authorization and can be interrupted.

### Industrial safety gate — future
Only simulation/digital-twin proof is accepted before physical adapter approval. No test can obtain credit by bypassing certified interlocks or replacing safety-controller authority with model reasoning.

## 18. Architectural conclusion

The intended Nika is not a bigger chatbot and not a brittle macro recorder.

It is a durable, user-owned autonomy control plane capable of combining deterministic software, local and cloud models, semantic/diagnostic/visual computer interaction, reusable connectors, specialist agents, self-improvement experiments and eventually product/voice/physical execution under one persistent policy/audit/runtime system.

The user's desired simplicity is the output of internal sophistication: the user should be able to state a difficult goal and receive the result while Nika handles ordinary diagnostics, discovery, adaptation, scheduling, recovery and maintenance itself. Complexity becomes visible only when the user requests expert detail or when a material exception genuinely requires the user's authority.