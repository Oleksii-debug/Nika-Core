# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, inspect current official documentation and maintained upstream projects. Record REUSE, ADAPT or CUSTOM. Prefer package dependencies/adapters over vendored source.

## M1 decisions — 2026-08-18
- REUSE — Pydantic + Pydantic Settings for typed/versioned configuration and `NIKA_*` environment loading.
- REUSE — Python `sqlite3` for the local deterministic store and transactional writes.
- CUSTOM (thin) — ordered SQLite schema migrations. At the current small local-only schema, Alembic would add SQLAlchemy/migration complexity without a corresponding benefit. Re-evaluate Alembic when schema transforms become complex or another SQL backend is introduced.
- REUSE — Python `importlib.metadata.entry_points()` as the installed-workspace discovery mechanism. Nika owns only the stable workspace contract and entry-point group.
- CUSTOM — Agent/Workspace registries, Action Registry/Keymap and Audit Log because they encode Nika-specific versioning, accessibility, safety and product policy.

## M2 runtime decision — 2026-08-18
- ADAPT — LangGraph as the primary durable orchestration runtime behind Nika `AgentRuntimePort`.
- REUSE — `langgraph-checkpoint-sqlite` for the first local durable checkpoint adapter/proof.
- KEEP AS SECONDARY CANDIDATE — Microsoft Agent Framework. Its Python core is production/stable and its workflows provide checkpointing, HITL and multi-agent patterns, but the native Python Ollama package remains prerelease and the current local checkpoint story is less directly aligned with Nika's SQLite-first desktop target.
- CUSTOM (thin) — Nika runtime contracts, normalized events/results, capability registry and selection boundary. These intentionally prevent LangGraph or Microsoft framework types from leaking into Nika domain APIs.

The dated comparison and required executable proof are in `docs/RUNTIME_SELECTION.md`. Do not run several orchestration kernels in production simultaneously without measured benefit. Re-run the selection gate if upstream stability, persistence or provider support materially changes.

Deep Agents, LiteLLM, MCP Python SDK, APScheduler and DSPy remain candidates for their planned milestones and must be re-verified immediately before adoption.

## Windows UI
ADAPT — pywebview + EdgeChromium/WebView2 with local HTML/CSS/JS. Reuse the Accessible Chess WebView2 accessibility-host lessons. React + TypeScript + Vite + React Aria Components remain the M5 frontend candidate subject to a fresh audit.

## Packaging
ADAPT — PyInstaller is the first pywebview Windows freezing path; Nuitka remains a measured fallback. Do not package every development cycle.

## Mandatory pre-code record
Every new subsystem decision is classified REUSE, ADAPT or CUSTOM. CUSTOM requires a short explanation of why maintained upstream options do not satisfy the requirement.
