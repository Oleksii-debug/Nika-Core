# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, inspect current official documentation and maintained upstream projects. Record REUSE, ADAPT or CUSTOM. Prefer package dependencies/adapters over vendored source.

## M1 decisions — 2026-08-18
- REUSE — Pydantic + Pydantic Settings for typed/versioned configuration and `NIKA_*` environment loading.
- REUSE — Python `sqlite3` for the local deterministic store and transactional writes.
- CUSTOM (thin) — ordered SQLite schema migrations. At the current small local-only schema, Alembic would add SQLAlchemy/migration complexity without a corresponding benefit. Re-evaluate Alembic when schema transforms become complex or another SQL backend is introduced.
- REUSE — Python `importlib.metadata.entry_points()` as the installed-workspace discovery mechanism. Nika owns only the stable workspace contract and entry-point group.
- CUSTOM — Agent/Workspace registries, Action Registry/Keymap and Audit Log because they encode Nika-specific versioning, accessibility, safety and product policy.

## Agent runtime selection gate
Do not lock the domain to one orchestration framework before M2 proof evidence. Current primary candidates are LangGraph and Microsoft Agent Framework. Microsoft Agent Framework is the forward Microsoft foundation incorporating AutoGen/Semantic Kernel experience and now documents workflows, checkpoint/resume, human-in-the-loop and multi-agent patterns. Nika domain will depend on `AgentRuntimePort`; concrete framework types must remain behind adapters.

Deep Agents, LiteLLM, MCP Python SDK, APScheduler and DSPy remain candidates for their planned milestones and must be re-verified immediately before adoption.

## Windows UI
ADAPT — pywebview + EdgeChromium/WebView2 with local HTML/CSS/JS. Reuse the Accessible Chess WebView2 accessibility-host lessons. React + TypeScript + Vite + React Aria Components remain the M5 frontend candidate subject to a fresh audit.

## Packaging
ADAPT — PyInstaller is the first pywebview Windows freezing path; Nuitka remains a measured fallback. Do not package every development cycle.

## Mandatory pre-code record
Every new subsystem decision is classified REUSE, ADAPT or CUSTOM. CUSTOM requires a short explanation of why maintained upstream options do not satisfy the requirement.
