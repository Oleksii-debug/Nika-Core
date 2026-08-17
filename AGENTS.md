# Nika Core autonomous development instructions

This repository is the canonical source of truth. Chat history is not.

Before every coding cycle read: docs/MASTER_SPEC.md, docs/ROADMAP.md, docs/THIRD_PARTY_ADOPTION.md, docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md, docs/ACCEPTANCE_GATES.md, state/PROJECT_STATUS.md, LIVE DASHBOARD, open PRs and current CI.

Primary rule: REUSE BEFORE REWRITE. Search maintained upstream libraries and current official documentation before implementing a subsystem. Record REUSE, ADAPT or CUSTOM. Do not copy random third-party source into this repository when a package dependency/adapter is sufficient.

Architecture: Windows-first modular monolith; LangGraph primary orchestration runtime; Nika owns task/audit/permission/product contracts; provider-neutral ModelGateway; workspace/plugin boundaries; deterministic code for state/validation/dedup/safety; LLM only where semantic reasoning is useful.

Accessibility: blind primary user, Windows 11 + NVDA. Standard controls, accessible names/roles, logical tab order, keyboard-only, text logs, no mouse-only flow. Automated accessibility tests do not equal human NVDA verification.

Safety: no secrets in repo; no token/session/browser profile files; dangerous send/delete/publish/financial/code-execution actions require preview/audit/approval as specified. Runtime agents never self-modify production source directly.

Git discipline: main must remain releasable. Use feature/fix branches and coherent commits. Never claim success without exact test evidence. Distinguish IMPLEMENTED, INTEGRATED, PACKAGED, HUMAN_TESTED.

CI cost: cheap Linux tests frequently; Windows hosted runner only for milestone/Windows-specific/release gates. Do not rebuild EXE every hourly cycle.

At the end of every cycle update canonical GitHub status with branch/SHA, changes, tests, blocker, weighted milestone progress and next coherent batch.
