# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, inspect current official documentation and maintained upstream projects. Record **REUSE**, **ADAPT**, **CUSTOM (thin)** or **REJECT/CAUTION**. Prefer package dependencies/adapters over vendored source.

The full cross-roadmap inventory is `docs/REUSE_CATALOG_2026-08-18.md`. It applies to M3–M12 in parallel and supersedes the old sequential “do not implement later milestones” instruction.

## Product boundary
Nika owns stable task/agent/workspace identity, lifecycle, permissions, approvals, audit evidence, provider/tool/runtime ports, accessibility semantics, plugin compatibility and release/recovery policy. Mature generic engines remain replaceable components behind these boundaries.

## Integrated foundation
### M1
- REUSE — Pydantic + Pydantic Settings for typed/versioned configuration and `NIKA_*` environment loading.
- REUSE — Python `sqlite3` for authoritative local deterministic state and transactional writes.
- CUSTOM (thin) — ordered SQLite migrations while the schema remains small/local. Re-evaluate Alembic only if schema transforms/backends justify it.
- REUSE — Python `importlib.metadata.entry_points()` for installed plugin/workspace discovery.
- CUSTOM (thin) — Agent/Workspace registries, Action Registry/Keymap and Audit Log because they encode Nika-specific versioning, accessibility and safety semantics.

### M2
- ADAPT / INTEGRATED — LangGraph behind Nika `AgentRuntimePort`.
- REUSE / INTEGRATED — `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite` for local durable checkpoints.
- KEEP AS SECONDARY — Microsoft Agent Framework as a migration/interop candidate, not a second production kernel without measured benefit.
- CUSTOM (thin) — runtime contracts/events, RuntimeSessionStore, IdempotencyLedger, RuntimeRecoveryService and Nika retry safety because external frameworks cannot know product task/approval/side-effect truth.
- REUSE/ADAPT — framework checkpoint/thread persistence remains execution truth; Nika stores only the product mapping/recovery evidence it owns.

Details remain in `docs/RUNTIME_SELECTION.md`, `docs/RUNTIME_RECOVERY_AND_SIDE_EFFECTS.md` and `docs/STARTUP_RECOVERY.md`.

## M3 decisions — memory, scheduling, resources
- REUSE — APScheduler for calendar/interval/trigger scheduling; Nika must not invent a scheduler engine. Keep a Nika `SchedulerPort`, job identity and persistence/audit mapping.
- REUSE — psutil for Windows/process CPU, memory, disk/network and process/resource observation (BSD-3-Clause).
- REUSE — SQLite for authoritative memory metadata, retention, consent and scheduler mappings.
- ADAPT OPTIONAL — Qdrant Python client for semantic/vector memory only. Local mode can run without a server and later switch to service mode. Vector search must never be authoritative task/runtime state.
- CUSTOM (thin) — memory namespaces/scopes, retention/consent rules, resource budgets and fairness.

## M4 decisions — models, tools, MCP
- ADAPT — LiteLLM for broad provider normalization only through the exact permissive/compatible package surface adopted at implementation time; keep optional.
- REUSE — HTTPX for direct OpenAI-compatible/local HTTP providers and bounded transport.
- CUSTOM (thin) — direct Ollama adapter remains appropriate because the local protocol is small and should not require a broad cloud-provider dependency.
- REUSE — official MCP Python SDK for MCP protocol/client/server behavior.
- CUSTOM (thin) — provider/model registry, routing/fallback policy, normalized usage/latency/cost/audit and tool risk decisions.

## Digital-worker / computer interaction
See `docs/COMPUTER_INTERACTION_REUSE_AUDIT.md`.
- ADAPT — Microsoft UFO² as first Windows computer-use proof candidate.
- REUSE — Playwright as deterministic semantic browser baseline.
- ADAPT OPTIONAL — Browser Use only if a proof shows meaningful reduction in glue beyond Playwright.
- REUSE FALLBACK — direct UI Automation/pywinauto adapter if UFO² is too heavy or cannot be safely isolated.
- VISION POLICY — screenshot/OCR/vision grounding is fallback when semantics are missing, not default control.
- CUSTOM (thin) — Nika capability/permission/evidence ports only.

## Software Factory and offline intelligence
See `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`.
- ADAPT — OpenHands SDK/agent-server as first coding-worker proof, limited to compatible permissively licensed surfaces.
- ADAPT — Unified Planning for explicit deterministic planning domains.
- REUSE — ONNX Runtime for compact specialist inference.
- REUSE — scikit-learn for measured classical ML tasks.
- REUSE — Gymnasium for controlled simulation/RL environment interfaces.
- ADAPT OPTIONAL — DSPy only for explicit metric/eval-backed optimization.
- CUSTOM (thin) — Nika coding/planning/experiment ports and production change safety.

## M5 Windows UI
- ADAPT — pywebview + EdgeChromium/WebView2 with local frontend.
- REUSE/ADAPT — React + TypeScript + Vite subject to packaging proof.
- REUSE — React Aria Components where they improve accessible names/focus/keyboard behavior.
- REUSE — Accessible Chess WebView2 accessibility-host lessons.
- HUMAN GATE — only a real NVDA test can award NVDA VERIFIED.

## M6–M9 agent/team/learning/plugin layer
- REUSE — Pydantic schemas and structured outputs for Agent Builder drafts; Nika owns permission truth.
- REUSE/ADAPT — LangGraph graph/subgraph primitives for multi-agent orchestration.
- REFERENCE ONLY unless a measured gap exists — Agent Zero, CrewAI, Agno and similar alternative orchestration platforms. Do not operate several competing kernels merely because they exist.
- REUSE — Python entry points + MCP SDK for plugin/workspace discovery/connectors.
- ADAPT — OpenHands for Software Factory, UFO² for Windows interaction, Playwright for browser interaction.
- CUSTOM (thin) — Nika parent/child identity, privilege attenuation, quotas, typed handoffs, experiment records and plugin compatibility contracts.

## Speech, OCR and vision optional workers
- ADAPT OPTIONAL — sherpa-onnx for offline speech recognition/TTS/VAD/keyword and related speech functions; keep models outside base installation.
- ADAPT OPTIONAL — faster-whisper where Whisper transcription is the better measured fit.
- REUSE OPTIONAL — Tesseract for mature OCR fallback.
- REUSE/ADAPT OPTIONAL — PaddleOCR for heavier multilingual OCR/document parsing.
- REUSE — OpenCV for deterministic vision preprocessing/primitives.
- ADAPT OPTIONAL — UI-TARS/OmniParser only after exact component/model license and Windows proof; semantics remain preferred over coordinate clicking.

## M10 security/reliability
- REUSE — Python keyring for OS-backed credential storage; Windows credential backend preferred over plaintext persistent config.
- REUSE — Yelp detect-secrets for secret baseline/new-secret prevention.
- REUSE — pip-audit for Python dependency vulnerability checks.
- REUSE SELECTIVELY — Tenacity for bounded adapter/network retry mechanics; Nika decides whether replay is safe.
- REUSE SELECTIVELY — watchdog for filesystem event monitoring instead of polling.
- REUSE SELECTIVELY — structlog for structured logs if standard logging becomes insufficient.
- REUSE — psutil for process/resource observation and controlled process-management evidence where needed.
- CUSTOM (thin) — R0–R4 policy, approvals, sandbox policy, secret references, backup/restore and fail-closed recovery.

## M11 packaging
- ADAPT — PyInstaller as first Windows freezing path; Nuitka remains measured fallback.
- Keep heavy models/workers optional and outside the base package unless a release explicitly requires them.
- CUSTOM (thin) — release manifest, checksums, optional-component manager and migration/backup hooks.

## M12 QA/release
- REUSE — pytest, coverage, Ruff, pip-audit, detect-secrets and GitHub Actions Windows+Ubuntu checks.
- REUSE/ADAPT — Playwright for semantic UI/web E2E tests where applicable.
- CUSTOM (thin) — Nika recovery drills, release matrix and human NVDA acceptance protocol.

## Licensing/copying policy
- Do not wholesale-copy third-party repositories into Nika Core.
- Pin exact versions/commits when a proof graduates to adoption; record license/notices and tests.
- GPL/restrictive/mixed-license components do not enter the distributed base without a separate decision.
- Modern AutoGPT/platform surfaces, newer Piper variants and mixed-model vision packages require explicit license review before any distribution decision.

## Parallel-first rule
Reuse research is not a reason to serialize the roadmap. Each M3–M12 lane performs its own fresh upstream/version/license check before adding a dependency, while source proofs proceed in isolated branches. Dependencies constrain integration order, not independent research or adapter implementation.

## Mandatory pre-code record
Every new subsystem decision must state why a maintained upstream package is reused/adapted or why a custom implementation is still necessary. “CUSTOM” without that explanation is a defect.
