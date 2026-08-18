# Nika Core — final technical baseline

Version 1.5, 2026-08-18. Windows 11 x64, NVDA-first.

## Product definition
Nika Core is one modular Windows platform, not a collection of unrelated scripts. It contains Nika Kernel, Agent Lab, Model Gateway, Agent Builder, Plugin/Workspace SDK, accessible web-style desktop UI and the personal Nika agent. Planned workspaces include GrantScanner/Universal Research, Telegram, YouTube Research, Transcription, Business Agent Lab, My Corrector, Product Search, Table Tennis Stats, AI Trader research, Model Engineering Lab and future adapters.

Agent Lab is a controlled digital-worker platform, not primarily a chatbot/search wrapper. The end-state agent can perceive structured interfaces, act through approved tools, use audio/vision specialist capabilities, retain scoped memory, delegate to specialists and learn from measured evidence. The user's practical model is intentional: eyes, hands, ears, mouth, memory and a brain — all implemented as replaceable controlled capabilities rather than one monolithic agent.

## End-state user capabilities
The final product must let the user install one normal Windows application without Python; create/import workspaces; create agents from natural language/templates; assign goals/tools/limits/success criteria; run one-shot or long-running tasks and agent teams; pause/restart/resume after app or PC failure; inspect state/log/audit/artifacts; choose no-LLM/mock, Ollama or cloud providers through one gateway; connect standardized tools; operate browsers and Windows applications through controlled semantic interaction adapters; run experiments and promote only verified strategies; approve dangerous actions; remap application shortcuts; and operate the complete UI with keyboard and NVDA.

## Architecture principles
1. Modular monolith first; no premature microservices.
2. Stable versioned ports/contracts separate Nika domain from frameworks/providers/UI shells.
3. Deterministic state, validation, deduplication, scheduling and safety outside the LLM.
4. Provider-neutral model access only through ModelGateway.
5. SQLite is the first canonical local state store with explicit ordered migrations.
6. Durable execution and restart recovery are first-class requirements.
7. Workspace/plugin boundaries are versioned contracts and independently discoverable.
8. Dangerous actions fail closed and require policy/approval/audit.
9. Accessibility is an acceptance gate from the first GUI build.
10. REUSE BEFORE REWRITE: maintained upstream libraries are preferred to custom infrastructure. The binding cross-roadmap adoption map is `docs/REUSE_CATALOG_2026-08-18.md`.
11. Self-learning may change memory/prompts/strategies/experiment candidates; production source changes only through isolated branch/sandbox, tests, CI, integration and release gates.
12. All application-specific hotkeys use a central Action Registry/Keymap and are remappable.
13. Primary desktop UI is local HTML/CSS/JS inside a Windows WebView2 shell, with Accessible Chess host-accessibility lessons incorporated from the start.
14. Computer interaction is capability-oriented: structured APIs/semantic trees first, vision and coordinates only as fallback.
15. Heavy coding/browser/vision/model workers are optional adapters/components; Nika Core remains the control plane rather than vendoring whole external platforms.
16. M3–M12 source development is PARALLEL-FIRST: dependencies constrain integration order, not independent research, adapter work, fixtures, mocks or tests.

## Core services
Config Service; Workspace Registry; Agent Registry; Task Queue/state machine; Agent Runtime/Orchestrator; SchedulerPort; Tool Registry; Permission Engine; Approval Engine; Event/Audit Log; Checkpoint/Resume; Memory Service; Resource Manager; Artifact Registry; ModelGateway; Plugin SDK; Action Registry/Keymap; Diagnostics/Health; Backup/Restore; future Computer Interaction Layer and Software Factory worker adapters.

## Canonical agent loop
Observe -> Plan -> Validate -> Act -> Record -> Evaluate -> Adapt -> Checkpoint -> Continue/Stop/Escalate. Every loop is bounded by max steps, deadline, cancellation and resource budget.

## Runtime selection — integrated M2 truth
Nika domain depends on `AgentRuntimePort`, never directly on a third-party orchestration framework. M2 completed the runtime proof/selection and integrated LangGraph as the primary implementation behind that port, including local durable resume/crash recovery, explicit approval, cancellation, bounded retry, side-effect idempotency/reconciliation and startup recovery. Microsoft Agent Framework remains a secondary migration/interop candidate. Do not operate several competing orchestration kernels in production without new measured evidence that a concrete requirement cannot be met through the current port.

## Configuration and persistence
Typed application configuration uses Pydantic/Pydantic Settings with `NIKA_*` environment overrides and validated defaults. Local authoritative data uses SQLite. Schema changes are explicit ordered migrations; a database newer than the running application fails closed rather than being modified blindly.

## Workspaces/plugins
Workspace definitions are versioned and persisted. Installed external workspaces are discovered using standard Python package entry points under the `nika_core.workspaces` group, but loading/activation remains governed by Nika validation, permissions and compatibility checks.

## Model Gateway
Use a stable Nika interface. Provider normalization may use LiteLLM where appropriate and license/package scope is compatible. Required providers: deterministic mock/no-LLM; direct Ollama; generic OpenAI-compatible/cloud adapter. HTTPX and provider SDKs are reused where appropriate rather than rebuilding transport. Model calls have timeout, cancellation, error normalization, privacy class and optional cost/usage accounting.

## Tools and MCP
Use the official MCP Python SDK for external tool/resource interoperability where useful. MCP never bypasses Nika permission/approval rules. Tools have stable IDs, schemas, side-effect classification, timeout, retry/idempotency metadata and audit hooks. Generic retry mechanics may reuse a maintained retry library, but Nika owns the decision whether an external side effect is safe to replay.

## Computer Interaction Layer
A dedicated layer gives agents controlled perception/action without binding Agent Lab to one automation framework.

Perception/action priority:
1. application/native API where available;
2. web DOM/accessibility semantics or Windows UI Automation/accessibility tree;
3. deterministic GUI actions against named semantic controls;
4. screenshot/OCR/vision grounding when semantics are missing;
5. coordinate mouse/keyboard only as a last-resort fallback.

Current reuse direction after the 2026-08-18 audit:
- Microsoft UFO²: first Windows AgentOS proof candidate behind a Nika Windows interaction adapter;
- Playwright: deterministic browser interaction baseline using role/label/user-visible semantic locators;
- Browser Use: optional higher-level browser-agent adapter only if it measurably reduces glue code;
- direct Windows UIA/pywinauto-style adapter: smaller fallback if UFO² cannot be isolated safely.

Nika retains permissions, approval, audit, cancellation, idempotency, task state and accessibility explanation. Third-party interaction objects never become domain types. Full decisions and gates are in `docs/COMPUTER_INTERACTION_REUSE_AUDIT.md`.

## Accessibility Repair Agent
When NVDA cannot expose a site/application, Nika should inspect DOM/UIA/accessibility evidence first, supplement with screenshot/vision only where required, explain the interface in text, and act only under applicable permission. Repeated inaccessible workflows should be converted into narrow versioned helpers/adapters/skills in sandbox rather than permanently relying on unexplained coordinate clicks.

## Software Factory
A Software Factory workspace turns an end-product goal into acceptance criteria, reuse research, architecture, isolated implementation, tests, accessibility review and release evidence. Nika coordinates the project and roles; it does not need to reimplement an entire coding-agent platform.

OpenHands Software Agent SDK/agent-server is the first coding-worker proof candidate behind a future framework-neutral `CodingWorkerPort`. Only permissively licensed core/SDK surfaces are candidates by default; enterprise-licensed components require a separate explicit decision. Coding workers modify isolated workspaces/worktrees/branches and return patches/commits/test evidence. They never write directly to production main.

Full reuse decision and proof gates are in `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`.

## Offline/minimal intelligence
No-LLM mode remains useful but is not represented as GPT-level general reasoning. It may combine deterministic workflows/state machines, rules, search/ranking, formal automated planning for explicit domains, classical ML, compact specialist ONNX models and experiment-driven strategy selection.

Current reuse candidates:
- Unified Planning behind a deterministic planner port where goals/actions/preconditions/effects are explicitly modeled;
- ONNX Runtime for local compact specialist model inference;
- scikit-learn for measured classical ML tasks with concrete datasets/metrics;
- OpenCV for deterministic computer-vision preprocessing;
- Gymnasium for controlled simulation/RL environment interfaces.

These are optional capability adapters, not mandatory dependencies merely to claim that Nika contains AI.

## Speech, audio, OCR and vision specialists
Large media/model stacks stay optional. Current reuse candidates include sherpa-onnx for broad offline speech capabilities, faster-whisper for Whisper-oriented transcription, Tesseract for mature OCR fallback, PaddleOCR for heavier multilingual/document OCR and OpenCV for deterministic preprocessing. Model files live outside the base application package and are selected through measured task-specific proofs.

## Task states
CREATED, READY, RUNNING, WAITING_TOOL, WAITING_APPROVAL, PAUSED, RETRYING, BLOCKED, COMPLETED, FAILED, CANCELLED, ARCHIVED. All transitions are explicit events. Retryable steps require idempotency/dedup rules.

## Memory
Separate short-term task state, conversation/thread state, agent memory, workspace knowledge and user-approved long-term memory. Namespaces prevent cross-workspace leakage. SQLite remains authoritative for Nika product state. Qdrant/local vector search is an optional semantic retrieval adapter when retrieval tests justify it and never replaces transactional state.

## Scheduler/resources
APScheduler is the current reuse direction behind SchedulerPort; do not implement a scheduling engine from scratch. Resource Manager owns Nika budgets/fairness but may reuse psutil for Windows/process CPU, memory, disk/network and process observation. Resource Manager limits concurrency, CPU/RAM-heavy jobs and browser/model/transcription overlap for the target Windows machine.

## Agent Builder
Natural-language request -> structured draft -> schema validation -> permission/tool review -> user confirmation where required -> versioned activation. First versions create configuration, not arbitrary executable Python. Pydantic/schema/structured-output facilities are reused; Nika owns permission truth and activation.

## Multi-agent laboratory
Support supervisor/subagents, router/fan-out, typed handoffs/messages, evaluator/critic agents and bounded parallelism. Reuse LangGraph graph/subgraph primitives where they satisfy execution needs. Prefer one optimized agent when multi-agent adds no measurable value; do not adopt another full orchestration kernel merely for a pattern already available through the current runtime.

## Controlled self-learning
Experiment Registry stores dataset/corpus version, strategy/prompt version, model/provider, seed where applicable, metrics and artifacts. Champion/challenger promotion requires explicit metrics and held-out/replay evidence. Failed challengers roll back automatically. No hidden autonomous production-code rewrite. DSPy may be adapted only where an explicit metric/evaluation dataset exists.

For trading/gambling research, autonomous learning is limited by default to backtest/paper/demo/simulation. Any future real-money/real-wager connector is separate, disabled by default and requires additional high-risk human approval gates.

## Windows web-style UI and accessibility
Baseline: pywebview with EdgeChromium/WebView2 on Windows, local HTML/CSS/JavaScript assets and a narrow validated JS/Python bridge. React/TypeScript/Vite and React Aria Components are reuse candidates for the frontend where packaging and accessibility proofs justify them. Native semantic HTML first; accessible names/roles/value, headings/landmarks, logical Tab order, keyboard-only navigation, predictable focus restore, copyable errors and text logs. Packaged WebView2 host accessibility is tested explicitly. Automated semantics are not human NVDA verification.

## Keyboard customization
A central Action Registry assigns stable IDs to application commands. Default bindings are configuration, not implementation constants in random UI code. Users can reassign, clear, restore, export/import and validate shortcuts. Standard editing keys remain standard in editable controls unless deliberately scoped.

## Security
No API keys, OAuth credentials, token/session files, cookies, browser profiles or private logs in Git. Tool permissions are least-privilege. File/shell access is sandboxed/restricted. Send/delete/publish/financial/legal/high-impact operations require preview/audit/approval as applicable. Prefer OS-backed credential storage through a maintained keyring adapter for persistent secrets. Reuse secret-scanning and dependency-audit tooling in repository/CI gates rather than relying only on ignore patterns.

## Reliability and filesystem observation
Use maintained process/filesystem/logging primitives where useful: psutil for process/resource observation; watchdog for filesystem event monitoring instead of polling; structured logging libraries only if standard logging no longer satisfies normalized event/audit requirements. Nika still owns recovery state, fail-closed policy and user-facing diagnostics.

## Packaging
Development is Python-first for speed. For the WebView shell, PyInstaller is the initial packaging path; Nuitka remains an evaluated alternative. Build diagnosable standalone/one-dir before optional one-file. Do not rebuild EXE on every development cycle. Final release must run without Python installed.

Heavy local models, coding-worker sandboxes, browser/vision stacks and media models are separable optional components. Updating Nika Core must not require redistributing every model or worker runtime.

## Release truth states
PREPARED != IMPLEMENTED != GREEN != INTEGRATED != PACKAGED != HUMAN_TESTED != NVDA_VERIFIED. Only an exact tested SHA may become a candidate. Real NVDA verification is performed by the user on Windows/NVDA and is never inferred from automated tests.
