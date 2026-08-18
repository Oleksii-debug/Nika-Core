# Nika Core autonomous development instructions

This repository is the canonical source of truth. Chat history is not.

Before every coding cycle read: docs/MASTER_SPEC.md, docs/ROADMAP.md, docs/THIRD_PARTY_ADOPTION.md, docs/UI_ARCHITECTURE.md, docs/LARGE_BATCH_POLICY.md, docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md, docs/ACCEPTANCE_GATES.md, state/PROJECT_STATUS.md, LIVE DASHBOARD, open PRs and current CI.

Primary rule: REUSE BEFORE REWRITE. Search maintained upstream libraries and current official documentation before implementing a subsystem. Record REUSE, ADAPT or CUSTOM. Do not copy random third-party source into this repository when a package dependency/adapter is sufficient.

Architecture: Windows-first modular monolith with ports/adapters and versioned contracts. Nika owns task/audit/permission/product contracts; provider-neutral ModelGateway; workspace/plugin boundaries; deterministic code for state/validation/dedup/safety; LLM only where semantic reasoning is useful. Agent orchestration must sit behind `AgentRuntimePort`; do not leak framework types into Nika domain APIs.

Agent-runtime rule: before M2 locks a primary framework, run the documented proof/selection gate comparing current LangGraph and current Microsoft Agent Framework on durable local resume, crash recovery, approvals, teams/subagents, MCP/tools, Ollama/provider independence, cancellation, observability, glue-code size, maintenance and licensing.

Accessibility: blind primary user, Windows 11 + NVDA. Web-style desktop UI uses local semantic HTML inside pywebview/WebView2; keyboard-only operation, accessible names/roles, headings/landmarks, deterministic focus and text logs are mandatory. Packaged WebView2 UI Automation discovery is a specific gate. Automated accessibility tests do not equal human NVDA verification.

Hotkeys: every application command has a stable Action Registry ID and all application-specific shortcuts are user-remappable through the Keymap system. Do not scatter hard-coded shortcuts through UI code or break standard editing keys.

Safety: no secrets in repo; no token/session/browser profile files; dangerous send/delete/publish/financial/code-execution actions require preview/audit/approval. Runtime agents never self-modify production source directly.

Git discipline: main must remain releasable. Use feature/fix branches and coherent commits. Never claim success without exact test evidence. Distinguish IMPLEMENTED, INTEGRATED, PACKAGED, HUMAN_TESTED.

CI cost: cheap source/unit/static checks during development; GitHub Actions on coherent PR/main/milestone gates. Windows hosted runner only for Windows/WebView2/package/release evidence. Do not rebuild EXE every hourly cycle.

At the end of every cycle update canonical GitHub status with branch/SHA, changes, tests, blocker, weighted milestone progress and next large coherent batch.
