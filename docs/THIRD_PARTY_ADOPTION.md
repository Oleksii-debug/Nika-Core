# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, inspect current official documentation and maintained upstream projects. Record **REUSE**, **ADAPT**, **CUSTOM (thin)** or **REJECT/CAUTION**. Prefer package dependencies/adapters over vendored source.

The broad inventory remains `docs/REUSE_CATALOG_2026-08-18.md`. Newer intelligence decisions are binding in `docs/INTELLIGENCE_REUSE_2026-08-19.md`; expanded end-state scope is binding in `docs/FULL_PRODUCT_VISION_2026-08-19.md`.

## Product boundary
Nika owns stable task/agent/workspace identity, lifecycle, permissions, approvals, audit evidence, provider/tool/runtime/planner ports, accessibility semantics, plugin compatibility, Product Journey semantics and release/recovery policy. Mature generic engines remain replaceable components behind these boundaries.

## Integrated foundation
### M1
- REUSE — Pydantic + Pydantic Settings for typed/versioned configuration and `NIKA_*` environment loading.
- REUSE — Python `sqlite3` for authoritative local deterministic state and transactional writes.
- CUSTOM (thin) — ordered SQLite migrations while the schema remains small/local.
- REUSE — Python `importlib.metadata.entry_points()` for installed plugin/workspace discovery.
- CUSTOM (thin) — Agent/Workspace registries, Action Registry/Keymap and Audit Log because they encode Nika-specific semantics.

### M2
- ADAPT / INTEGRATED — LangGraph behind Nika `AgentRuntimePort`.
- REUSE / INTEGRATED — `langgraph-checkpoint-sqlite` + `aiosqlite` for local durable checkpoints.
- KEEP AS SECONDARY — Microsoft Agent Framework as migration/interop candidate, not a second production kernel without measured benefit.
- CUSTOM (thin) — runtime contracts/events, session/idempotency/recovery mappings and Nika retry safety.

## Memory, scheduling and resources
- REUSE — APScheduler behind SchedulerPort.
- REUSE — psutil for Windows/process resource observation.
- REUSE — SQLite for authoritative memory metadata, retention, consent and scheduler mappings.
- ADAPT OPTIONAL — Qdrant for semantic/vector memory only when measured retrieval evidence justifies it; never authoritative task/runtime state.
- CUSTOM (thin) — memory scopes, retention/consent, resource budgets/fairness and future power profiles.

## Intelligence and Model Gateway
Nika now treats model-free planning, embedded models, external local model servers and cloud APIs as separate replaceable capabilities.

### Deterministic Brain — ADAPT Unified Planning
- ADAPT — `unified-planning` 1.3.x with a small compatible engine such as Pyperplan for explicit Boolean/state/action planning.
- Nika owns `WorldState`, goal/action and planner contracts; Unified Planning types stay inside the adapter.
- Planned actions execute through the existing guarded ToolExecutor and therefore do not bypass approval/security policy.
- REUSE — SQLite FTS5 and ordinary deterministic libraries/search before adding semantic models.
- REUSE — scikit-learn only for measured classical ML tasks with explicit datasets/metrics.

### Embedded Brain — ADAPT Microsoft Foundry Local
- PRIMARY WINDOWS ADAPT — official Microsoft Foundry Local Python SDK.
- Windows optional package: `foundry-local-sdk-winml>=1.2.3,<2`; cross-platform package only where appropriate.
- Use the SDK directly for embedded/in-process inference; do not require the optional local HTTP server for the normal Nika path.
- Keep Foundry-specific types behind ModelGateway.
- Large model files remain separate optional artifacts with model-specific license/checksum/resource evidence.
- No silent model download during ordinary inference.
- Physical Windows inference proof is required before full product acceptance; mock/provider contract tests alone are not sufficient.
- Current upstream docs explicitly document cancellation for model/EP downloads but not hard cancellation of active inference. Do not claim a guarantee that has not been measured/proven.

### Embedded alternatives
- KEEP AS FALLBACK — llama.cpp / maintained adapter for GGUF/CPU/Vulkan/Windows cases that win a measured proof.
- KEEP AS LOWER-LEVEL FALLBACK — ONNX Runtime GenAI for direct generative ONNX inference when a measured need justifies the more volatile/lower-level API.
- REUSE — ONNX Runtime for compact specialist models, not as a general reasoning claim.

### Existing local/cloud routes
- CUSTOM (thin) / INTEGRATED — direct Ollama adapter remains appropriate because its local protocol is small and predictable.
- REUSE — HTTPX for direct OpenAI-compatible/local HTTP providers and bounded transport.
- ADAPT OPTIONAL — LiteLLM for broad provider normalization only where its exact adopted package/license surface and dependency cost are justified.
- CUSTOM (thin) — provider registry, routing/fallback/privacy/cost policy, normalized usage/latency/audit.

## Tools and MCP
- REUSE — official MCP Python SDK for MCP protocol/client/server behavior.
- CUSTOM (thin) — Nika tool IDs, risk policy, idempotency/retry decision and approval/audit semantics.

## Digital-worker / computer interaction
See `docs/COMPUTER_INTERACTION_REUSE_AUDIT.md`.
- ADAPT — Microsoft UFO² as first Windows computer-use proof candidate.
- REUSE — Playwright as deterministic semantic browser baseline.
- ADAPT OPTIONAL — Browser Use only if a proof shows meaningful reduction in glue beyond Playwright.
- REUSE FALLBACK — direct UI Automation/pywinauto adapter if UFO² is too heavy or cannot be safely isolated.
- VISION POLICY — screenshot/OCR/vision grounding is fallback when semantics are missing, not default control.
- CUSTOM (thin) — Nika capability/permission/evidence ports only.

## Software Factory / Toolsmith
See `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`.
- ADAPT — OpenHands SDK/agent-server as first coding-worker proof, limited to compatible permissively licensed surfaces.
- CUSTOM (thin) — CodingWorkerPort, capability-gap record, branch/workspace safety, acceptance evidence and resume linkage to the original task.
- Toolsmith must search existing/upstream capabilities before generating custom code.

## Windows UI
- ADAPT — pywebview + EdgeChromium/WebView2 with local frontend.
- REUSE/ADAPT — React + TypeScript + Vite only where packaging/accessibility proof justifies them.
- REUSE — React Aria Components where they improve accessible name/focus/keyboard behavior.
- REUSE — Accessible Chess WebView2 accessibility-host lessons.
- HUMAN GATE — only a real NVDA test can award NVDA VERIFIED.

## Agent/team/learning/plugin layer
- REUSE — Pydantic schemas/validators and structured outputs for Agent Builder drafts; Nika owns permission truth.
- REUSE/ADAPT — LangGraph graph/subgraph primitives for multi-agent orchestration.
- REFERENCE ONLY unless a measured gap exists — Agent Zero, CrewAI, Agno and similar alternative orchestration platforms. Do not operate several competing kernels merely because they exist.
- REUSE — Python entry points + MCP SDK for plugin/workspace discovery/connectors.
- ADAPT — OpenHands for Software Factory, UFO² for Windows interaction, Playwright for browser interaction.
- ADAPT OPTIONAL — DSPy only for explicit metric/eval-backed optimization.
- CUSTOM (thin) — parent/child identity, privilege attenuation, quotas, typed handoffs, experiment records and plugin compatibility contracts.

## Universal Research / document layer
- HTTP/API first with HTTPX where a stable endpoint exists.
- REUSE — pypdf, python-docx, openpyxl and robust HTML/XML extractors for their formats rather than hand-parsing commodity formats.
- REUSE — SQLite FTS5 and RapidFuzz where a concrete search/dedup workflow requires them.
- REUSE — Playwright for dynamic semantic browser flows.
- CUSTOM (thin) — source identity/freshness/provenance, workspace card schemas, evidence/confidence, dedup policy and review state.

## Speech, OCR and vision optional workers
- ADAPT OPTIONAL — sherpa-onnx for offline speech recognition/TTS/VAD/keyword features where measured.
- ADAPT OPTIONAL — faster-whisper for Whisper-focused transcription.
- REUSE OPTIONAL — Tesseract for mature OCR fallback.
- REUSE/ADAPT OPTIONAL — PaddleOCR for heavier multilingual/document parsing.
- REUSE — OpenCV for deterministic vision preprocessing/primitives.
- ADAPT OPTIONAL — UI-TARS/OmniParser only after exact component/model license and Windows proof; semantics remain preferred over coordinate clicking.

## Security/reliability
- REUSE — Python keyring for OS-backed credential storage.
- REUSE — detect-secrets and pip-audit in security gates.
- REUSE SELECTIVELY — Tenacity for bounded adapter/network retries while Nika decides whether replay is safe.
- REUSE SELECTIVELY — watchdog for filesystem events rather than polling.
- REUSE SELECTIVELY — structlog if standard logging becomes insufficient.
- REUSE — psutil for process/resource observation and controlled termination evidence.
- CUSTOM (thin) — R0–R4 policy, approvals, sandbox policy, secret references, backup/restore and fail-closed recovery.

## Packaging
- ADAPT — PyInstaller as first Windows freezing path; Nuitka remains measured fallback.
- Heavy models/workers are optional and outside the base package unless a release explicitly requires them.
- Model/application updates are separable so unchanged model files are not re-downloaded with every Nika update.
- CUSTOM (thin) — release manifest, checksums, optional-component manager and migration/backup hooks.

## QA/release
- REUSE — pytest, coverage, Ruff, pip-audit, detect-secrets and GitHub Actions Windows+Ubuntu checks.
- REUSE/ADAPT — Playwright and Windows UIA proofs for semantic E2E where applicable.
- CUSTOM (thin) — Product Journey tests, recovery drills, release matrix and human NVDA acceptance protocol.

## Removed active scope
Telegram is removed from the active roadmap by user decision on 2026-08-19. Historical Telethon/TDLib decisions do not justify adding a Telegram dependency or workspace to current Nika. A future explicit request would require a fresh adoption decision like any other optional workspace.

## Licensing/copying policy
- Do not wholesale-copy third-party repositories into Nika Core.
- Pin exact versions/commits when a proof graduates to adoption; record license/notices and tests.
- Model licenses are audited independently of their inference engine's license.
- GPL/restrictive/mixed-license components do not enter the distributed base without a separate decision.
- Do not adopt a component solely because it is popular; require concrete Nika capability, maintenance, license fit and proof.

## Parallel-first rule
Reuse research does not serialize the roadmap. Each lane performs fresh upstream/version/license checks before dependency graduation while source proofs proceed in isolated branches. Dependencies constrain integration order, not independent proof work.

## Mandatory pre-code record
Every new subsystem decision must state why a maintained upstream package is reused/adapted or why custom implementation is still necessary. “CUSTOM” without that explanation is a defect.
