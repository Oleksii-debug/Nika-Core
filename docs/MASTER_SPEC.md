# Nika Core — final technical baseline

Version 1.3, 2026-08-18. Windows 11 x64, NVDA-first.

## Product definition
Nika Core is one modular Windows platform, not a collection of unrelated scripts. It contains Nika Kernel, Agent Lab, Model Gateway, Agent Builder, Plugin/Workspace SDK, accessible web-style desktop UI and the personal Nika agent. Planned workspaces include GrantScanner/Universal Research, Telegram, YouTube Research, Transcription, Business Agent Lab, My Corrector, Product Search, Table Tennis Stats, AI Trader research, Model Engineering Lab and future adapters.

## End-state user capabilities
The final product must let the user install one normal Windows application without Python; create/import workspaces; create agents from natural language/templates; assign goals/tools/limits/success criteria; run one-shot or long-running tasks and agent teams; pause/restart/resume after app or PC failure; inspect state/log/audit/artifacts; choose no-LLM/mock, Ollama or cloud providers through one gateway; connect standardized tools; run experiments and promote only verified strategies; approve dangerous actions; remap application shortcuts; and operate the complete UI with keyboard and NVDA.

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
10. REUSE BEFORE REWRITE: maintained upstream libraries are preferred to custom infrastructure.
11. Self-learning may change memory/prompts/strategies/experiment candidates; production source changes only through isolated branch/sandbox, tests, CI, integration and release gates.
12. All application-specific hotkeys use a central Action Registry/Keymap and are remappable.
13. Primary desktop UI is local HTML/CSS/JS inside a Windows WebView2 shell, with Accessible Chess host-accessibility lessons incorporated from the start.

## Core services
Config Service; Workspace Registry; Agent Registry; Task Queue/state machine; Agent Runtime/Orchestrator; SchedulerPort; Tool Registry; Permission Engine; Approval Engine; Event/Audit Log; Checkpoint/Resume; Memory Service; Resource Manager; Artifact Registry; ModelGateway; Plugin SDK; Action Registry/Keymap; Diagnostics/Health; Backup/Restore.

## Canonical agent loop
Observe -> Plan -> Validate -> Act -> Record -> Evaluate -> Adapt -> Checkpoint -> Continue/Stop/Escalate. Every loop is bounded by max steps, deadline, cancellation and resource budget.

## Runtime selection
Nika domain depends on `AgentRuntimePort`, never directly on a third-party orchestration framework. Before M2 locks a primary runtime, perform a proof/selection batch comparing current LangGraph and current Microsoft Agent Framework on local durable resume, crash recovery, approvals, teams/subagents, MCP/tools, Ollama/provider independence, async/cancellation, observability, glue-code size, maintenance and licensing. Select one primary implementation; keep alternatives behind adapters/research only.

## Configuration and persistence
Typed application configuration uses Pydantic/Pydantic Settings with `NIKA_*` environment overrides and validated defaults. Local data uses SQLite. Schema changes are explicit ordered migrations; a database newer than the running application fails closed rather than being modified blindly.

## Workspaces/plugins
Workspace definitions are versioned and persisted. Installed external workspaces are discovered using standard Python package entry points under the `nika_core.workspaces` group, but loading/activation remains governed by Nika validation, permissions and compatibility checks.

## Model Gateway
Use a stable Nika interface. Provider normalization may use LiteLLM where appropriate. Required providers: deterministic mock/no-LLM; Ollama; generic OpenAI-compatible/cloud adapter. Model calls have timeout, cancellation, error normalization, privacy class and optional cost/usage accounting.

## Tools and MCP
Use the official MCP Python SDK for external tool/resource interoperability where useful. MCP never bypasses Nika permission/approval rules. Tools have stable IDs, schemas, side-effect classification, timeout, retry/idempotency metadata and audit hooks.

## Task states
CREATED, READY, RUNNING, WAITING_TOOL, WAITING_APPROVAL, PAUSED, RETRYING, BLOCKED, COMPLETED, FAILED, CANCELLED, ARCHIVED. All transitions are explicit events. Retryable steps require idempotency/dedup rules.

## Memory
Separate short-term task state, conversation/thread state, agent memory, workspace knowledge and user-approved long-term memory. Namespaces prevent cross-workspace leakage. Vector search is optional and introduced only when retrieval tests justify it.

## Scheduler/resources
APScheduler remains the current implementation candidate behind SchedulerPort. Resource Manager limits concurrency, CPU/RAM-heavy jobs and browser/model/transcription overlap for the target 16 GB Windows machine.

## Agent Builder
Natural-language request -> structured draft -> schema validation -> permission/tool review -> user confirmation where required -> versioned activation. First versions create configuration, not arbitrary executable Python.

## Multi-agent laboratory
Support supervisor/subagents, router/fan-out, typed handoffs/messages, evaluator/critic agents and bounded parallelism. Prefer one optimized agent when multi-agent adds no measurable value.

## Controlled self-learning
Experiment Registry stores dataset/corpus version, strategy/prompt version, model/provider, seed where applicable, metrics and artifacts. Champion/challenger promotion requires explicit metrics and held-out/replay evidence. Failed challengers roll back automatically. No hidden autonomous production-code rewrite.

## Windows web-style UI and accessibility
Baseline: pywebview with EdgeChromium/WebView2 on Windows, local HTML/CSS/JavaScript assets and a narrow validated JS/Python bridge. Native semantic HTML first; accessible names/roles/value, headings/landmarks, logical Tab order, keyboard-only navigation, predictable focus restore, copyable errors and text logs. Packaged WebView2 host accessibility is tested explicitly. Automated semantics are not human NVDA verification.

## Keyboard customization
A central Action Registry assigns stable IDs to application commands. Default bindings are configuration, not implementation constants in random UI code. Users can reassign, clear, restore, export/import and validate shortcuts. Standard editing keys remain standard in editable controls unless deliberately scoped.

## Security
No API keys, OAuth credentials, token/session files, cookies, browser profiles or private logs in Git. Tool permissions are least-privilege. File/shell access is sandboxed/restricted. Send/delete/publish/financial/legal/high-impact operations require preview/audit/approval as applicable.

## Packaging
Development is Python-first for speed. For the WebView shell, PyInstaller is the initial packaging path; Nuitka remains an evaluated alternative. Build diagnosable standalone/one-dir before optional one-file. Do not rebuild EXE on every development cycle. Final release must run without Python installed.

## Release truth states
IMPLEMENTED != INTEGRATED != PACKAGED != HUMAN_TESTED. Only exact tested SHA may become a candidate. Real NVDA verification is performed by Oleksii on Windows/NVDA and is never inferred from automated tests.
