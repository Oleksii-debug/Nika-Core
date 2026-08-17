# Nika Core — final technical baseline

Version 1.1, 2026-08-17. Windows 11 x64, NVDA-first.

## Product definition
Nika Core is one modular Windows platform, not a collection of unrelated scripts. It contains: Nika Kernel (deterministic core), Agent Lab (autonomous agents and experiments), Model Gateway (all model providers), Agent Builder, Plugin/Workspace SDK, accessible desktop UI, and the personal Nika agent. Planned workspaces include GrantScanner/Universal Research, Telegram, YouTube Research, Transcription, Business Agent Lab, My Corrector, Product Search, Table Tennis Stats, AI Trader research, Model Engineering Lab and future adapters.

## End-state user capabilities
The final product must let the user install one normal Windows application without Python; create/import workspaces; create agents from natural language or templates; assign goals/tools/limits/success criteria; run one-shot or long-running tasks and agent teams; pause/restart/resume after app or PC failure; inspect state/log/audit/artifacts; choose no-LLM/mock, Ollama or cloud providers through one gateway; connect standardized external tools; run experiments and promote only verified strategies; approve dangerous actions; and operate the complete UI with keyboard and NVDA.

## Architecture principles
1. Modular monolith first; no premature microservices.
2. Deterministic state, validation, deduplication, scheduling and safety outside the LLM.
3. Provider-neutral model access only through ModelGateway.
4. One canonical local state model; SQLite first.
5. Durable execution and restart recovery are first-class requirements.
6. Workspace/plugin boundaries are versioned contracts.
7. Dangerous actions fail closed and require policy/approval/audit.
8. Accessibility is an acceptance gate from the first GUI build.
9. Reuse before rewrite: maintained upstream libraries are preferred to custom infrastructure.
10. Self-learning may change memory, prompts, strategies and experiment candidates; production source changes only through isolated branch/sandbox, tests, CI, integration and release gates.

## Core services
Config Service; Workspace Registry; Agent Registry; Task Queue and state machine; Agent Runtime/Orchestrator; SchedulerPort; Tool Registry; Permission Engine; Approval Engine; Event/Audit Log; Checkpoint/Resume; Memory Service; Resource Manager; Artifact Registry; ModelGateway; Plugin SDK; Diagnostics/Health; Backup/Restore.

## Canonical agent loop
Observe -> Plan -> Validate -> Act -> Record -> Evaluate -> Adapt -> Checkpoint -> Continue/Stop/Escalate. Every loop is bounded by max steps, deadline, cancellation and resource budget.

## Runtime adoption
LangGraph is the primary durable orchestration runtime. Nika retains its own product/domain task IDs, permissions and audit history around LangGraph. `langgraph-checkpoint-sqlite` supplies local graph checkpoints. Deep Agents may provide reusable planning, subagent, filesystem/memory and permission harness features behind Nika interfaces. AutoGen and CrewAI are evaluated alternatives/adapters, not simultaneous kernel runtimes.

## Model Gateway
Use a stable Nika interface. Implement provider normalization with LiteLLM where appropriate. Required providers: deterministic mock/no-LLM; Ollama; generic OpenAI-compatible/cloud adapter. The known local Ollama profile is `http://localhost:11434`, qwen3:8b, with prior working settings `stream=false`, `think=false`; these values are configuration, never hard-coded secrets. Model calls have timeout, cancellation, error normalization, privacy class and optional cost/usage accounting.

## Tools and MCP
Use the official MCP Python SDK v2 as the interoperability layer for external tools/resources where useful. MCP access never bypasses Nika permission/approval rules. Tools have stable IDs, schemas, side-effect classification, timeout, retry/idempotency metadata and audit hooks.

## Task states
CREATED, READY, RUNNING, WAITING_TOOL, WAITING_APPROVAL, PAUSED, RETRYING, BLOCKED, COMPLETED, FAILED, CANCELLED, ARCHIVED. All transitions are explicit events. Retryable steps require idempotency/dedup rules.

## Memory
Separate short-term task state, conversation/thread state, agent memory, workspace knowledge and user-approved long-term memory. Namespaces must prevent cross-workspace leakage. Vector search is optional and introduced only when retrieval tests justify it.

## Scheduler and resources
Use APScheduler stable 3.x behind SchedulerPort; do not expose its classes in public contracts. Resource Manager limits concurrency, CPU/RAM-heavy jobs and browser/model/transcription overlap for the target Ryzen 5 7430U/16 GB machine.

## Agent Builder
Natural-language request -> structured draft -> schema validation -> permission/tool review -> user confirmation where required -> versioned activation. First versions create configuration, not arbitrary executable Python.

## Multi-agent laboratory
Support supervisor/subagents, router/fan-out, typed handoffs/messages, evaluator/critic agents and bounded parallelism. Prefer one optimized agent when multi-agent adds no measurable value.

## Controlled self-learning
Experiment Registry stores dataset/corpus version, strategy/prompt version, model/provider, seed where applicable, metrics and artifacts. Champion/challenger promotion requires explicit metrics and held-out/replay evidence. DSPy may later optimize prompt/program behavior against fixed metrics. Failed challengers roll back automatically. No hidden autonomous production code rewrite.

## Windows UI and accessibility
Baseline: PySide6 Qt Widgets. Standard controls, QAccessible/UI Automation semantics, accessible name/role/value, logical Tab order, keyboard-only navigation, predictable focus restore, copyable error details and text logs. No status communicated by color alone. Automated semantics are not human NVDA verification.

## Security
No API keys, OAuth credentials, token/session files, cookies, browser profiles or private logs in Git. `.env`/local config is excluded. Tool permissions are least-privilege. File/shell access is sandboxed/restricted. Send/delete/publish/financial/legal/high-impact operations require preview/audit/approval as applicable.

## Packaging
Development is Python-first for speed. Use `pyside6-deploy`/Nuitka to produce standalone Windows `.exe`/distribution at milestone, user-test and release gates. Do not rebuild EXE on every hourly code change. Final releases contain executable, config templates, docs/licenses, changelog, manifest and SHA-256 checksums and must run without Python installed.

## Release truth states
IMPLEMENTED != INTEGRATED != PACKAGED != HUMAN_TESTED. Only exact tested SHA may become a candidate. Real NVDA verification is performed by Oleksii on Windows/NVDA and is never inferred from automated UIA tests.
