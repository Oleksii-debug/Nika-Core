# Nika Core — final technical baseline

Version 1.7, 2026-08-20. Windows 11 x64, NVDA-first.

The expanded end-state capability scope is binding in `docs/FULL_PRODUCT_VISION_2026-08-19.md`. The Autonomous Product Factory clarification is binding in `docs/AUTONOMOUS_PRODUCT_FACTORY.md`, `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` and `docs/AUTONOMOUS_BUSINESS_FACTORY.md`. Historical/core milestone progress and Full Product Vision readiness are separate truths.

## Product definition
Nika Core is one modular Windows platform, not a collection of unrelated scripts. It contains Nika Kernel, Agent Lab, Model Gateway, Deterministic Brain, Embedded Brain adapters, Agent Builder, Plugin/Workspace SDK, accessible web-style desktop UI, the personal Nika agent and the reusable control-plane services required by Autonomous Product Factory.

Active/future workspaces may include GrantScanner/Universal Research, YouTube Research, Transcription, Business Agent Lab, My Corrector, Product Search, Table Tennis Stats, AI Trader research, Model Engineering Lab and future user-created adapters/workspaces. These are not a requirement to prebuild every possible product into Nika. The strategic rule is **build the factory, not every possible product**: complex user goals may become durable ProductProjects that Nika researches, designs, implements, tests, deploys and maintains.

**Telegram is not an active roadmap workspace.** Historical documents that mention Telegram/Telethon/TDLib are non-binding unless the user explicitly requests a new Telegram workspace/ProductProject in the future.

Agent Lab is a controlled digital-worker platform, not primarily a chatbot/search wrapper. The end-state agent can perceive structured interfaces, act through approved tools, use audio/vision specialist capabilities, retain scoped memory, delegate to specialists, obtain missing capabilities through a controlled Toolsmith/Software Factory loop, compose project-specific teams and learn from measured evidence. The practical model is intentional: eyes, hands, ears, mouth, memory and multiple replaceable kinds of “brain” rather than one monolithic model.

## End-state user capabilities
The final product must let the user install one normal Windows application without Python; create/import workspaces; create agents from natural language/templates; assign goals/tools/limits/success criteria; run one-shot or long-running tasks and agent teams; pause/restart/resume after app or PC failure; inspect state/log/audit/artifacts; choose **model-free deterministic intelligence, embedded Foundry Local intelligence, external local servers such as Ollama, or allowed cloud/API providers**; connect standardized tools; operate browsers and Windows applications through controlled semantic interaction adapters; run experiments and promote only verified strategies; request/build a missing capability through Toolsmith/Software Factory; create and manage durable ProductProjects from natural-language product goals; optionally research a market/problem before development; compose project-specific specialist teams; create/connect one or several repositories; coordinate implementation, independent QA, accessibility review, build/package, approved deployment and post-release maintenance; use Business Factory for user-approved lawful market/opportunity workflows; remap application shortcuts; and operate the complete UI with keyboard and NVDA.

## Architecture principles
1. Modular monolith first; no premature microservices.
2. Stable versioned ports/contracts separate Nika domain from frameworks/providers/UI shells.
3. Deterministic state, validation, deduplication, scheduling, permissions and safety remain outside language models.
4. Provider-neutral model access only through ModelGateway.
5. **Deterministic Brain is first-class and must work with no language model at all.**
6. **Embedded Brain is optional and replaceable; Microsoft Foundry Local is the primary Windows implementation candidate/integration, not a new product kernel.**
7. SQLite is the first canonical local state store with explicit ordered migrations.
8. Durable execution and restart recovery are first-class requirements.
9. Workspace/plugin boundaries are versioned contracts and independently discoverable.
10. Dangerous/high-impact actions fail closed and remain governed by policy/approval/audit. User-defined standing permissions may reduce repetitive confirmation inside a bounded declared scope but may not silently widen themselves or bypass mandatory high-impact boundaries.
11. Accessibility is an acceptance gate from the first GUI build.
12. REUSE BEFORE REWRITE: maintained upstream libraries are preferred to custom infrastructure. The binding adoption map is `docs/REUSE_CATALOG_2026-08-18.md`.
13. Self-learning may change memory/prompts/strategies/experiment candidates and propose tools; production source changes only through isolated branch/sandbox, tests, CI, integration and release gates.
14. All application-specific hotkeys use a central Action Registry/Keymap and are remappable.
15. Primary desktop UI is local HTML/CSS/JS inside a Windows WebView2 shell, reusing Accessible Chess host-accessibility lessons.
16. Computer interaction is capability-oriented: structured APIs/semantic trees first, vision and coordinates only as fallback.
17. Heavy coding/browser/vision/model workers and model files are optional components; Nika Core remains the control plane.
18. Development is dependency-aware PARALLEL-FIRST: dependencies constrain integration order, not independent research/adapter/test work.
19. **A backend subsystem is not product-complete until its final packaged user journey is wired and proven.**
20. Core readiness and Full Product Vision readiness are reported separately.
21. **Large digital-product goals are durable ProductProjects, not oversized transient AgentTasks or CodingJobs.**
22. **Team composition is dynamic and scope/risk driven; agent count is not a quality metric.**
23. **A software product is not complete at source-code generation; required build, release, deployment and maintenance gates are part of Product Factory truth.**
24. **Secrets/accounts are accessed through a Credential/Identity Broker using opaque/scoped references; raw persistent credentials do not belong in prompts, model memory, Git or ordinary logs.**
25. **Competitor/market research may inform independent product design, but Product Factory must preserve IP/license/compliance provenance and may not treat access to proprietary code/assets as permission to copy them.**

## Core services
Config Service; Workspace Registry; Agent Registry; Task Queue/state machine; Agent Runtime/Orchestrator; Deterministic Brain / DeterministicPlannerPort; SchedulerPort; Tool Registry/ToolExecutor; Permission Engine; Approval Engine; Event/Audit Log; Checkpoint/Resume; Memory Service; Knowledge/Corpus boundary; Resource Manager; Artifact Registry; ModelGateway; embedded-model adapter boundary; Plugin SDK; Action Registry/Keymap; Diagnostics/Health; Backup/Restore; Computer Interaction Layer; Software Factory/CodingWorkerPort; Capability Escalation/Toolsmith coordinator; shared accessible report/artifact layer; ProductProject Service; ProductRepositoryGraph; Dynamic Team Composer; Deployment Fabric; Execution Node Registry/Remote Build boundary; Credential/Identity Broker; Product Operations/Maintenance Service; Business Factory orchestration boundary.

## Canonical agent loop
Observe -> Plan -> Validate -> Act -> Record -> Evaluate -> Adapt -> Checkpoint -> Continue/Stop/Escalate. Every loop is bounded by max steps, deadline, cancellation and resource budget. `Escalate` includes a capability-gap path: find/reuse/adapt/build/test/register the needed tool safely, then resume the original task from checkpoint when successful.

A ProductProject is a higher-level durable lifecycle above individual agent loops. It may survive many worker runs and application restarts while retaining decisions, requirements, repositories, milestones, releases and maintenance state.

## Runtime selection — integrated M2 truth
Nika domain depends on `AgentRuntimePort`, never directly on a third-party orchestration framework. LangGraph is the primary implementation behind that port, including durable resume/crash recovery, explicit approval, cancellation, bounded retry, side-effect idempotency/reconciliation and startup recovery. Microsoft Agent Framework remains a secondary migration/interop candidate. Do not run several competing orchestration kernels in production without a measured requirement.

## Configuration and persistence
Typed application configuration uses Pydantic/Pydantic Settings with `NIKA_*` environment overrides and validated defaults. Local authoritative data uses SQLite. Schema changes are explicit ordered migrations; a database newer than the running application fails closed rather than being modified blindly.

ProductProject persistence must use versioned Nika-owned contracts and preserve goal/spec/decision/repository/team/release/operations identities across restart. A long-running project must not require reconstructing context from chat history.

## Workspaces/plugins
Workspace definitions are versioned and persisted. Installed external workspaces are discovered using standard Python package entry points under `nika_core.workspaces`; loading/activation remains governed by Nika validation, permissions and compatibility checks. Generic capabilities belong in reusable core ports/adapters rather than being copied into every workspace.

A ProductProject may produce or consume workspaces/plugins, or may build an independent external product when that architecture is more appropriate. Workspaces are not the only possible output form of Product Factory.

## Intelligence architecture

### Deterministic Brain — zero model
This mode is not `DeterministicMockProvider`. It is executable model-free autonomy for structured domains. It combines explicit world state/goals/actions, deterministic workflows, formal planning, registered tools/APIs/libraries, local search/ranking/dedup, durable memory/checkpoints, classical statistical/ML components where measured, and experiment-driven strategy selection.

The first formal planning implementation adapts **Unified Planning** behind Nika-owned deterministic planning contracts. The planner may choose a registered action sequence but never bypasses ToolExecutor permission/approval decisions. This mode is expected to handle many research pipelines, known file/data workflows, replay/backtesting, scheduled routines and recovery procedures without Ollama, cloud API or an embedded LLM.

### Embedded Brain — Microsoft Foundry Local primary
`FoundryLocalProvider` integrates Microsoft Foundry Local behind the same ModelGateway contract and uses the official Python SDK directly for embedded/in-process local inference. On Windows, `foundry-local-sdk-winml` is the preferred optional package. Large model files remain separate optional components. Automatic model download is not silently triggered by ordinary inference; model installation/download is an explicit product action with model/version/license/checksum/resource evidence.

Alternative embedded backends remain replaceable candidates:
- llama.cpp / maintained adapter — GGUF/CPU/Vulkan/other portability fallback after measured Windows proof;
- ONNX Runtime GenAI — lower-level direct generative ONNX fallback when justified by a measured advantage;
- ONNX Runtime — compact specialist classifier/ranker/audio/vision inference, not itself a general reasoning agent.

Foundry-specific, llama.cpp-specific or ONNX-specific types never become Nika domain contracts. Actual physical-Windows inference proof is distinct from mock/SDK-import tests. Do not claim hard inference cancellation if the selected upstream SDK does not document/prove it.

### External local model server
Direct Ollama and generic local OpenAI-compatible servers remain supported. They are useful when the user manages/shares model services independently of Nika.

### Cloud/API intelligence
Generic OpenAI-compatible/cloud/provider-SDK adapters remain optional. LiteLLM may normalize broad provider coverage where its exact adopted surface is appropriate. Routing respects privacy class, user authorization, capability, cost/budget and resource policy.

## Model Gateway
ModelGateway is a stable Nika interface. Required architectural routes are: no-model/deterministic path, embedded local provider path, external local provider path, and optional cloud/API path. HTTPX/provider SDKs are reused rather than rebuilding transport. Model calls normalize errors/usage/latency and obey documented timeout/cancellation capability. Sensitive data cannot be routed to providers that are not permitted to handle it.

## Tools and MCP
Use the official MCP Python SDK for external tool/resource interoperability where useful. MCP never bypasses Nika permission/approval rules. Tools have stable IDs, schemas, side-effect classification, timeout, retry/idempotency metadata and audit hooks. Generic retry mechanics may reuse a maintained library, but Nika owns the decision whether an external side effect is safe to replay.

## Capability Escalation / Toolsmith
When an active task proves a missing capability, Nika records the gap, searches existing tools/plugins/upstream reusable components, and may ask Software Factory/CodingWorker for a narrowly scoped adapter/tool in an isolated branch/workspace. The candidate is tested and security/compatibility/accessibility-reviewed as applicable. Only then can it be registered under ordinary permissions, after which the original task resumes from checkpoint. Failure leaves the task safely blocked with evidence. Toolsmith never writes directly to production main and never expands its own permissions.

Toolsmith closes a narrow capability gap for an existing task. A large independent digital-product goal routes to ProductProject/Autonomous Product Factory instead of pretending to be one oversized Toolsmith job.

## Computer Interaction Layer
A dedicated layer gives agents controlled perception/action without binding Agent Lab to one automation framework.

Perception/action priority:
1. application/native API where available;
2. web DOM/accessibility semantics or Windows UI Automation/accessibility tree;
3. deterministic GUI actions against named semantic controls;
4. screenshot/OCR/vision grounding when semantics are missing;
5. coordinate mouse/keyboard only as a last-resort fallback.

Current reuse direction:
- Microsoft UFO²: first Windows AgentOS proof candidate behind a Nika Windows interaction adapter;
- Playwright: deterministic browser interaction baseline using role/label/user-visible semantic locators;
- Browser Use: optional higher-level browser-agent adapter only if it measurably reduces glue code;
- direct Windows UIA/pywinauto-style adapter: smaller fallback if UFO² cannot be isolated safely.

Nika retains permissions, approval, audit, cancellation, idempotency, task state and accessibility explanation. Third-party interaction objects never become domain types.

## Accessibility Repair Agent
When NVDA cannot expose a site/application, Nika inspects DOM/UIA/accessibility evidence first, supplements with screenshot/vision only where required, explains the interface in text, and acts only under applicable permission. Repeated inaccessible workflows should become narrow versioned helpers/adapters/skills in sandbox rather than permanent unexplained coordinate clicking.

Accessibility Repair is both a user-facing capability and a reusable Product Factory review/repair capability for products Nika creates.

## Software Factory / Autonomous Product Factory
A Software Factory workspace turns a bounded implementation goal into acceptance criteria, reuse research, architecture, isolated implementation, tests, accessibility review and release evidence. OpenHands Software Agent SDK/agent-server is the first coding-worker proof candidate behind framework-neutral `CodingWorkerPort`; Codex/other workers may be added as replaceable adapters when justified. Nika owns project state, branch policy, permissions, tests and release truth. Coding workers modify isolated workspaces/worktrees/branches and return patches/commits/test evidence; they do not write directly to production main.

For full products, the binding layer is `docs/AUTONOMOUS_PRODUCT_FACTORY.md`: ProductProject connects Research -> requirements -> architecture -> Dynamic Team Composer -> one/multi-repository implementation -> independent QA/accessibility -> build/package -> Deployment Fabric -> operations/maintenance. A product can span multiple platforms and execution nodes. Source generation alone is not completion.

Manual ChatGPT Deep Research developer chats may also act as **real implementation lanes** when the user runs them: they may read live GitHub, reason over a large subsystem, code on an owned branch and drive a coherent batch toward integration. Paired auditor chats independently review live evidence. When these manual lanes are active, scheduled workers should be paused or reassigned to complementary non-colliding QA/release/integration/evidence work rather than duplicating the same source ownership.

Full policy: `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`, `docs/FULL_PRODUCT_VISION_2026-08-19.md`, `docs/AUTONOMOUS_PRODUCT_FACTORY.md` and `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`.

## Autonomous Business Factory
Business Agent Lab is a reusable orchestration layer, not a hard-coded bot for one marketplace/niche. Binding design: `docs/AUTONOMOUS_BUSINESS_FACTORY.md`.

It may research markets/opportunities, compose business roles, qualify approved leads/work, prepare/send communication inside user-configured policy, turn work into ProductProjects, coordinate delivery and track payment/support state. It reuses Universal Research and Product Factory rather than duplicating crawler/coding/deployment stacks.

External communication, account creation/actions, contracting, publishing and money movement must comply with platform rules and Nika authorization. No spam, deceptive impersonation, prohibited automation or self-expansion of account/financial authority.

## Universal Research Engine
GrantScanner is a profile/workspace over a reusable research engine, not a one-off crawler. Shared capabilities include source identity/health, HTTP/API-first fetching, semantic browser fallback, document extraction, incremental freshness/change state, deterministic pre-filtering, optional intelligence routing, evidence/confidence, deduplication, structured cards, review state, scheduled reruns and accessible reports. Unchanged sources should not be repeatedly re-analyzed by expensive models.

Universal Research is also the research arm of Product Factory and Business Factory. Approved research evidence can become ProductProject/business-decision provenance without manual copy/paste.

## Corpus / Knowledge layer
Approved local knowledge is ingested with provenance/hash/version/workspace namespace, parsed with maintained format libraries, normalized/chunked/indexed, and searched deterministically through SQLite FTS5 before optional vector/semantic retrieval is added. Qdrant/local vector search is optional when retrieval evaluation proves benefit and never replaces transactional state. Knowledge retrieval enforces workspace/user permissions before material reaches any deterministic/local/cloud intelligence path.

## Speech, audio, OCR and vision specialists
Heavy model stacks stay optional. Candidates include sherpa-onnx for broad offline speech capabilities, faster-whisper for Whisper transcription, Tesseract for mature OCR fallback, PaddleOCR for heavier multilingual/document OCR and OpenCV for deterministic preprocessing. Model files live outside the base application package and are selected through measured task-specific proofs.

## Task states
CREATED, READY, RUNNING, WAITING_TOOL, WAITING_APPROVAL, PAUSED, RETRYING, BLOCKED, COMPLETED, FAILED, CANCELLED, ARCHIVED. All transitions are explicit events. Retryable steps require idempotency/dedup rules.

## Memory
Separate short-term task state, conversation/thread state, agent memory, workspace knowledge and user-approved long-term memory. Namespaces prevent cross-workspace leakage. SQLite remains authoritative for Nika product state.

ProductProject state is distinct from conversational memory: architecture decisions, requirements, repository/release identity and durable project history cannot depend on an LLM remembering a chat.

## Scheduler/resources
APScheduler remains behind SchedulerPort; do not implement a scheduler engine from scratch. Resource Manager owns budgets/fairness and may reuse psutil. It limits concurrency and heavy browser/model/transcription overlap and should evolve to resource profiles such as normal, battery/economy, night/heavy-batch and low-memory when measured on target Windows hardware.

Product Factory may additionally schedule approved remote/platform-specific execution nodes when local hardware/platform is insufficient. Execution nodes are replaceable resources behind Nika contracts.

## Agent Builder
Natural-language request -> structured draft -> schema validation -> permission/tool review -> user confirmation where required -> versioned activation. First versions create configuration, not arbitrary executable Python. Pydantic/schema/structured-output facilities are reused; Nika owns permission truth and activation.

## Multi-agent laboratory
Support supervisor/subagents, router/fan-out, typed handoffs/messages, evaluator/critic agents and bounded parallelism. Reuse LangGraph graph/subgraph primitives where they satisfy execution needs. Prefer one optimized agent when multi-agent adds no measurable value; do not adopt another full orchestration kernel merely for a pattern already available through the current runtime.

For ProductProjects, Dynamic Team Composer selects roles based on scope/risk/dependencies. It may consolidate several roles into one capable worker or fan out when independence is useful; it does not create agents merely to maximize count.

## Controlled self-learning
Experiment Registry stores dataset/corpus version, strategy/prompt version, model/provider, seed where applicable, metrics and artifacts. Champion/challenger promotion requires explicit metrics and held-out/replay evidence. Failed challengers roll back automatically. No hidden autonomous production-code rewrite. DSPy may be adapted only where an explicit metric/evaluation dataset exists.

For AI Trader or other financial/market research, start with backtest/paper/demo/simulation. Future live connectors are separate and governed by user-configured authorization profiles, budgets/scopes and the Nika risk/approval system. Standing authorization may reduce repetitive confirmations inside its exact declared scope, but agents cannot silently expand permissions/budgets or bypass mandatory high-impact boundaries.

## AI Trader workspace truth
Generic experiment/runtime infrastructure does **not** mean AI Trader is implemented. The future workspace must separately prove historical no-lookahead replay, time-ordered odds/event snapshots, virtual bank, singles/combinations/portfolio exposure, time waves, versioned strategies, drawdown/risk metrics, held-out evaluation, live/prematch paper trading, restart-safe sessions and accessible reports.

## Model Engineering Lab
Model Engineering Lab is a real future workspace for benchmarked comparison/management of embedded models, Ollama/local-server models, allowed cloud models, prompts/strategies, embeddings/retrieval settings and specialist models. Promotion uses explicit versioned metrics/held-out evidence through the Experiment Engine. Optional PEFT/LoRA-style adaptation is isolated and adopted only when hardware/licensing/metrics justify it.

## Windows web-style UI and accessibility
Baseline: pywebview with EdgeChromium/WebView2 on Windows, local HTML/CSS/JavaScript assets and a narrow validated JS/Python bridge. React/TypeScript/Vite and React Aria Components remain reuse candidates where packaging/accessibility proofs justify them. Native semantic HTML first; accessible names/roles/value, headings/landmarks, logical Tab order, keyboard-only navigation, predictable focus restore, copyable errors and text logs. Packaged WebView2 host accessibility is tested explicitly. Automated semantics are not human NVDA verification.

The final command center must expose ProductProject creation/status/decisions, team/repository/build/deployment state and Business Factory decisions through keyboard-reachable semantic controls or accessible text, not only hidden backend APIs.

## Product Journey completion rule
A user-facing capability is incomplete until the exact real path is proven:

`packaged Windows UI -> semantic action/command -> validated bridge/API -> real Nika service/runtime -> persisted state/result -> accessible visible feedback -> restart/resume where relevant`.

Backend-only tests, placeholder controls, mock lists or inaccessible packaged paths do not close the product gate. The 2026-08-19 disconnected task-action UI defect is canonical evidence for this rule.

For Product Factory, this rule is necessary but not sufficient: the product-level evidence in `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` must also pass.

## Keyboard customization
A central Action Registry assigns stable IDs to application commands. Defaults are configuration, not hard-coded scattered shortcuts. Users can reassign, clear, restore, export/import and validate shortcuts. Standard editing keys remain standard in editable controls unless deliberately scoped.

## Security
No API keys, OAuth credentials, token/session files, cookies, browser profiles or private logs in Git. Tool permissions are least-privilege. File/shell access is sandboxed/restricted. Send/delete/publish/financial/legal/high-impact operations remain governed by preview/audit/approval as applicable. Prefer OS-backed credential storage through a maintained keyring adapter. Reuse secret/dependency scanning in CI.

Product Factory and Business Factory use Credential/Identity Broker references rather than placing raw persistent secrets in ProductProject/workspace/task records or model prompts. Workers receive least-privilege scoped/short-lived credentials where supported; cannot enumerate unrelated credentials; and credential use is audited without serializing the secret.

Competitive research must preserve IP/license/compliance boundaries. Public functional/market research may inform independent design, but proprietary source/assets/credentials are not copied merely because they are technically accessible. Adopted dependencies/tools record version/license/provenance and distribution obligations.

## Reliability and filesystem observation
Use maintained process/filesystem/logging primitives where useful: psutil for process/resource observation; watchdog for filesystem event monitoring instead of polling; structured logging libraries only if standard logging becomes insufficient. Nika owns recovery state, fail-closed policy and user-facing diagnostics.

## Packaging
Development is Python-first for speed. PyInstaller is the initial WebView Windows packaging path; Nuitka remains evaluated fallback. Build diagnosable standalone/one-dir before optional one-file. Do not rebuild EXE on every development cycle. Final users must not need Python.

Heavy embedded/local models, coding-worker sandboxes, browser/vision stacks and media models are separable optional components. Updating Nika Core must not require redistributing every model, worker runtime or user dataset.

Independent products created by Product Factory have their own build/package/deployment truth; they are not automatically bundled into the Nika Core EXE.

## Release and scope truth states
PREPARED != IMPLEMENTED != GREEN != INTEGRATED != PACKAGED != HUMAN_TESTED != NVDA_VERIFIED. Only exact tested code/artifacts receive the corresponding evidence state. Real NVDA verification is human-only.

Historical milestone percentages describe the scoped Core roadmap that produced them. They do **not** automatically measure completion of the expanded Full Product Vision or Autonomous Product Factory. Until a new explicit weighted Full Product Vision roadmap is adopted, report concrete end-to-end capabilities rather than inventing a full-product percentage.
