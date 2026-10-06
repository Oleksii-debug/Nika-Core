# Nika Core — Full Product Vision

Updated: 2026-08-20.
Status: binding end-state product scope. This document expands the Core milestone roadmap; it does not retroactively award implementation credit.

## 1. Product truth

Nika Core is the reusable Windows/NVDA-first control plane for a much larger end-state personal agent platform. Core readiness and Full Product Vision readiness are different measurements.

- **Core readiness** measures the reusable kernel/runtime/memory/model/tool/UI/security/package infrastructure already proven by executable gates.
- **Full Product Vision readiness** measures whether the user can actually use the complete intended capabilities and real workspaces end to end from the packaged Windows application.
- A subsystem being implemented internally does not count as a finished user capability until its complete product journey is wired and proven.

The previous 98% number is historical Core-gate evidence. It must never be described as 98% completion of the expanded Full Product Vision.

The 2026-08-20 binding clarification is equally important: Nika is **not** supposed to prebuild every possible future vertical into Core. The end-state platform must become an autonomous digital product factory able to research, design, implement, test, deploy and maintain new products/workspaces when the user requests them. Binding details are in `docs/AUTONOMOUS_PRODUCT_FACTORY.md`, `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` and `docs/AUTONOMOUS_BUSINESS_FACTORY.md`.

## 2. End-state intelligence modes

Nika must remain one product while supporting four replaceable intelligence modes through stable Nika-owned contracts.

### 2.1 Deterministic Brain — no model at all

This is a first-class capability, not a mock LLM. It uses no Ollama, no cloud API and no embedded language model. It combines:

1. explicit world state, goals, rules, preconditions and effects;
2. deterministic workflows/state machines;
3. formal automated planning for modeled domains;
4. Nika Tool Registry actions and ordinary APIs/libraries;
5. local search, full-text retrieval, ranking, deduplication and provenance;
6. classical ML/statistical classifiers when a dataset and metric justify them;
7. memory/checkpoints and restart-safe execution;
8. experiment metrics and bounded strategy selection.

The first formal planner adapter is Unified Planning with a compatible engine such as Pyperplan. Nika owns the `DeterministicPlanner` contract and action/goal/world-state semantics. Unified Planning remains replaceable.

A deterministic plan never bypasses the existing ToolExecutor permission/approval boundary. The planner may decide *which* registered action is needed; Nika still decides whether that action is authorized and whether its side effect may execute.

This mode can be highly autonomous in explicit domains: crawling known sources, filtering/deduplicating, file workflows, report pipelines, replay/backtesting, scheduled routines, recovery procedures and many tool-based tasks. It is not falsely represented as open-ended GPT-level natural-language intelligence.

### 2.2 Embedded Brain — local model owned by the Nika installation

The primary Windows embedded-model implementation is **Microsoft Foundry Local** behind ModelGateway.

Binding integration rules:

- use the official Microsoft Python SDK directly for embedded/in-process inference rather than requiring a separate local HTTP server;
- use `foundry-local-sdk-winml` on Windows as the preferred package surface, with the cross-platform SDK only where appropriate;
- Foundry-specific objects never become Nika domain types;
- large model files are optional components stored outside the base EXE/ZIP;
- model download is explicit; Nika does not silently download multi-gigabyte models because an agent happened to request one;
- privacy-sensitive data may stay local under the same ModelGateway privacy contracts;
- model/version/license/checksum/resource requirements are recorded per installed model;
- actual Windows hardware inference must pass a focused proof before Foundry Local receives full production acceptance credit.

Alternative embedded backends remain deliberately available behind the same ModelGateway contract:

- **llama.cpp / a maintained Python binding or native adapter** — portability/fallback candidate, especially when GGUF models or CPU/Vulkan paths win a measured Windows proof;
- **ONNX Runtime GenAI** — lower-level fallback for direct ONNX generative inference where its evolving API provides a measured advantage;
- ordinary **ONNX Runtime** — specialist classifier/ranker/vision/audio inference, not a general reasoning engine.

Nika must not install all inference engines into the mandatory base merely to advertise compatibility. Each alternative graduates through version/license/Windows/performance/package tests.

### 2.3 External local model server

Ollama and compatible local servers remain supported through ModelGateway. They are useful when the user already maintains models independently, wants easy model switching, or another program shares the same local inference service.

### 2.4 Cloud/API intelligence

Cloud providers remain optional through the provider-neutral ModelGateway/OpenAI-compatible/provider-SDK layer. Nika may route a task to cloud only when policy/privacy/budget allow it. Provider choice must not rewrite the rest of the agent/runtime/tool system.

## 3. Capability Escalation / Toolsmith loop

A final Nika agent must not terminate a long task merely because a required capability is missing when the capability can safely be obtained.

Required loop:

1. the running task detects and records a concrete capability gap;
2. existing Tool Registry, plugin/workspace catalog and maintained upstream components are searched first;
3. if reuse/adaptation is insufficient, the Software Factory/CodingWorker is asked for a narrowly scoped implementation in an isolated branch/workspace;
4. generated/adapted code is executed only in the sandbox/isolated worker with declared file/network/process permissions;
5. deterministic tests, security checks, compatibility checks and relevant accessibility tests run;
6. a successful capability is registered/versioned under normal Nika permission rules;
7. the original task resumes from its checkpoint and uses the new capability;
8. failures leave the original task safely blocked with evidence rather than corrupting production state.

Self-improvement may create tools, strategies, prompts, adapters and experiment candidates. It does not silently rewrite production source or widen its own permissions.

## 4. Product Journey completion rule

A capability is not finished because a backend class and unit test exist. User-facing completion requires one continuous product journey:

`packaged Windows UI -> semantic user action -> validated bridge/API -> real Nika service/runtime -> persisted state/result -> visible accessible feedback -> restart/resume where relevant`.

Every user-facing capability must prove:

- the final packaged window exposes it through keyboard-reachable semantic controls or the command surface;
- the control calls the real implementation rather than a placeholder;
- success and failure are visible as copyable/readable text;
- state is durable where the operation is long-lived;
- restart/recovery behavior is tested when applicable;
- the packaged WebView2/UIA path remains discoverable;
- automated accessibility evidence is recorded;
- real NVDA verification is awarded only by the human user.

The 2026-08-19 Windows task-action defect is the canonical example of why this gate exists: internal services and accessible-looking controls can both exist while the final product journey is still disconnected.

## 5. Universal Research Engine

GrantScanner is no longer treated as a one-off crawler. Nika must have a reusable research/search pipeline that workspaces can configure with different source sets, schemas and relevance rules.

Shared capabilities:

- Source Manager with source identity, priority, authentication requirement and health;
- HTTP/API-first fetching, semantic browser fallback and document extraction;
- incremental crawling with last-seen/version/hash/freshness state;
- cheap deterministic pre-filter before expensive model analysis;
- optional Deterministic Brain, embedded model or cloud model analysis through stable contracts;
- evidence fragments and uncertainty/confidence for extracted conclusions;
- URL/title/content/fuzzy deduplication;
- structured cards governed by workspace-specific schemas;
- review status and provenance;
- scheduled reruns that show new/changed information rather than reprocessing unchanged pages;
- accessible DOCX/XLSX/CSV/TXT/HTML reporting.

First practical profiles may include grants, products, education/events and other structured opportunity searches. Telegram is not an active or required workspace.

Universal Research must also serve Product Factory and Business Factory. Research results must be able to become versioned ProductProject/business-opportunity evidence without manual copying.

## 6. Corpus and Knowledge layer

Nika must support an approved local knowledge corpus independent of any particular model:

- ingest supported local documents and workspace artifacts;
- parse structured text with maintained format libraries;
- preserve document identity, provenance, hash/version and workspace namespace;
- normalize/chunk/index for deterministic search;
- use SQLite FTS5 before adding semantic/vector retrieval;
- optionally use local embeddings/Qdrant only after measured retrieval benefit;
- enforce workspace/user permission scopes before retrieval;
- make retrieved evidence available to deterministic, embedded-local and cloud intelligence paths through the same Nika-owned knowledge interface.

## 7. Model Engineering Lab

Model Engineering Lab is a real future workspace, not only a note in the reuse catalog. It benchmarks and manages replaceable intelligence components:

- local embedded models, Ollama models and allowed cloud models;
- quality on versioned task/evaluation sets;
- latency, RAM/CPU/GPU use and package/runtime compatibility;
- prompt/strategy variants;
- embeddings/retrieval configurations;
- specialist models;
- optional measured PEFT/LoRA-style adaptation in isolated experiments where hardware and licensing permit it.

No model or prompt is promoted merely because it looks better in a few examples. Promotion uses explicit metrics and held-out/replay evidence through the existing Experiment Engine.

## 8. AI Trader workspace

AI Trader remains a future real workspace on top of the common Nika Core services. It is not counted as implemented merely because generic experiment and multi-agent infrastructure exists.

Required end-state research capabilities include:

- historical replay that hides future information at decision time;
- time-ordered odds/event snapshots;
- virtual bank and bankroll history;
- singles, combinations and portfolio-level exposure;
- time-wave grouping of events;
- versioned strategies expressed as data/configuration when possible;
- risk rules and drawdown metrics;
- repeated train/validation/held-out replay;
- live/prematch paper trading;
- deterministic/statistical learning as well as optional model-backed analysis;
- restart-safe sessions and accessible reports.

Financial autonomy is governed by explicit user-configured authorization profiles and Nika risk policy. A standing authorization may remove repetitive confirmations for actions inside its declared scope, limits and lifetime. Mandatory high-impact safety boundaries still apply; an agent cannot grant itself broader permissions or silently expand a budget.

## 9. Resource-aware local operation

Nika targets ordinary Windows hardware, including the user's Ryzen/16-GB integrated-GPU laptop. Heavy capabilities must be optional and coordinated.

Resource Manager should evolve beyond concurrency counts to profiles such as:

- normal;
- battery/economy;
- night/heavy batch;
- low-memory;
- model-active / transcription-active mutual exclusion where benchmarks justify it.

Nika should unload idle heavy models when useful, avoid unnecessary simultaneous Chromium/model/transcription jobs, and keep model/component caches separate from program updates.

Product Factory may also schedule authorized remote/platform-specific execution nodes when the target cannot reasonably be built on the local Windows machine.

## 10. Shared accessible report service

Workspaces should reuse a common report/artifact layer. Required output principles:

- DOCX with real heading hierarchy and simple tables;
- XLSX/CSV with clear column names and no accessibility-hostile merged-cell layouts by default;
- TXT/HTML for robust screen-reader access;
- source/provenance/evidence fields where analysis depends on external material;
- explicit error/uncertainty text rather than color-only status.

Product Factory additionally uses the same accessible artifact layer for requirements, architecture decisions, project status, test evidence, release notes, deployment evidence and maintenance reports.

## 11. Command center

The final Nika Windows application must provide a natural-language command surface in addition to structured screens. The command surface routes requests into versioned tasks/agents/workspaces/ProductProjects; it does not bypass validation or permissions.

Examples of intended behavior:

- create/configure an agent;
- start/inspect/pause/resume a long task;
- ask Universal Research to run a profile;
- run an AI Trader replay experiment;
- request a report;
- request a new capability through Toolsmith/Software Factory;
- create a durable ProductProject from a product goal;
- ask Nika to research a market/problem before proposing a product;
- approve a product direction and let Nika compose a development team;
- inspect ProductProject milestones, repositories, tests, builds and blockers;
- request an approved staging deployment/release;
- ask Business Factory to research a lawful business opportunity and convert an approved opportunity into a WorkOrder/ProductProject;
- choose or constrain the intelligence mode.

Ambiguous or unsafe commands are clarified or blocked before external side effects.

## 12. Autonomous Product Factory — binding end-state capability

Nika must be capable of managing the complete lifecycle of a new digital product rather than requiring every possible product to be manually prebuilt into Nika itself.

Binding architecture is defined in `docs/AUTONOMOUS_PRODUCT_FACTORY.md`. At minimum the end-state includes:

- first-class durable `ProductProject` identity/state;
- formal Research -> Product lifecycle;
- dynamic Team Composer;
- one- or multi-repository ProductRepositoryGraph;
- provider-neutral CodingWorker integration;
- independent QA/audit and accessibility review;
- build/package/release orchestration;
- Deployment Fabric;
- multi-platform/remote execution nodes;
- Credential/Identity Broker;
- post-release operations/maintenance;
- IP/license/compliance gates.

A messenger, social network, screen-reader-like product, browser-agent platform, business application or other complex software is an example of a possible ProductProject class. Nika is not required to hard-code those products into Core. Project size changes duration, resources and team composition; it does not change the lifecycle architecture.

Completion is proven through `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`, not by demonstrating that a coding worker can generate a few files.

## 13. Autonomous Business Factory — binding future orchestration layer

Business Agent Lab is expanded into a reusable Business Factory described in `docs/AUTONOMOUS_BUSINESS_FACTORY.md`.

It may research markets/opportunities, compose appropriate business roles, qualify leads/work, create proposals within policy, convert approved work into ProductProjects, coordinate delivery and track support/payment state. The business model is not hard-coded to one marketplace or niche.

External communication, account actions, contracts, publishing and money movement remain subject to user-configured authorization profiles, platform rules and audit. Agents cannot self-promote to broader financial/account authority.

## 14. Explicitly removed active scope

**Telegram is removed from the active Nika Core roadmap and active workspace catalog by user decision on 2026-08-19.** Historical documents may mention Telegram, Telethon or TDLib, but those mentions are not implementation requirements. If the user later asks Nika to build such a workspace/product, it is treated like any new optional ProductProject/workspace and goes through fresh research/reuse/security/product gates.

## 15. Development acceleration model

Manual ChatGPT Deep Research developer chats are allowed to be real coding lanes, not research-only lanes. A Deep Research developer may read the complete live project, reason over a large subsystem, implement code on its owned branch, run/trigger tests and drive one large coherent batch toward integration.

For each manual developer lane, an independent auditor chat may review live GitHub evidence, architecture, tests, security/accessibility impact and request corrections. Auditors do not accept a developer's prose summary as proof when GitHub evidence is available.

When manual developer/auditor lanes are active, scheduled autonomous workers should be paused or reassigned to complementary low-collision work such as integration QA, release/package proof, regression hunting, architecture/evidence consistency and cross-lane conflict detection. They should not duplicate the same source ownership merely to maximize the number of running agents.

For Product Factory development, source ownership must additionally be separated by ProductProject/component/repository. A central project coordinator owns dependency/integration truth; parallel workers own explicit non-overlapping implementation scopes.

## 16. Scope accounting

Future status reports must keep these truths separate:

- historical/core acceptance-gate progress;
- current integrated Core readiness after regressions/repairs;
- Full Product Vision capability readiness;
- Autonomous Product Factory readiness;
- Autonomous Business Factory readiness where active;
- packaged Windows candidate readiness;
- HUMAN_TESTED;
- NVDA_VERIFIED.

A precise Full Product Vision percentage may be introduced only after its expanded capability weights and acceptance gates are explicitly defined. Until then, report concrete finished/unfinished product journeys instead of inventing a percentage.
