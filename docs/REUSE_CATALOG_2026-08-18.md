# Nika Core — full reuse catalog (2026-08-18)

Status: architecture/adoption control document.  This catalog does **not** grant milestone credit by itself.

## Non-negotiable rule
Before writing a subsystem, search maintained upstream implementations and current official documentation.  Prefer, in order:

1. **REUSE** — consume a maintained package/API/SDK with minimal wrapper code.
2. **ADAPT** — place a third-party engine behind a Nika-owned stable port and normalize its data/events/errors.
3. **CUSTOM (thin)** — write only Nika-specific policy, contracts, accessibility semantics, durable mappings and integration glue that an upstream project cannot know.
4. **CUSTOM (substantial)** — allowed only after an explicit proof that no maintained upstream option satisfies the requirement.

Do not copy entire repositories into Nika Core.  Pin the adopted dependency/version at implementation time, record its license/notices and acceptance tests, and keep heavy/optional engines outside the mandatory base install.

## What Nika itself must own
The reusable-product boundary is deliberate. Nika owns the stable product semantics that must survive replacement of any one framework:
- task/agent/workspace identifiers and lifecycle;
- policy, R0–R4 permissions and human approval;
- durable product state, audit evidence and normalized events;
- provider/tool/runtime ports and compatibility versions;
- accessible Windows UX, Action Registry and Keymap semantics;
- plugin/workspace manifests and capability declarations;
- user-visible recovery, backup/restore and release evidence.

Everything else should be reused or adapted where practical.

## A–Z adoption matrix

### M0 — research, governance, bootstrap
- **REUSE** Pydantic / Pydantic Settings — typed configuration and validation (MIT).
- **REUSE** platformdirs — Windows-correct config/data/cache paths (MIT).
- **REUSE** pytest + pytest-cov — tests/coverage (MIT).
- **REUSE** Ruff — formatter/linter/static checks (MIT).
- **REUSE** pip-audit — dependency vulnerability audit (Apache-2.0).
- **REUSE** Yelp detect-secrets — repository secret baseline and new-secret prevention (Apache-2.0).
- **CUSTOM (thin)** Nika adoption records, acceptance gates and status truth because these are product governance.

### M1 — deterministic kernel
- **REUSE** Python sqlite3 — authoritative local transactional state.
- **REUSE** importlib.metadata entry points — installed plugin/workspace discovery.
- **CUSTOM (thin)** ordered SQLite migrations while schema is small; reconsider Alembic only if migrations/backends become complex.
- **CUSTOM (thin)** Agent/Workspace registries, Action Registry/Keymap and audit contracts; these are Nika-specific semantics.

### M2 — durable agent runtime
- **ADAPT / INTEGRATED** LangGraph behind `AgentRuntimePort` — durable orchestration/checkpoint model.
- **REUSE / INTEGRATED** langgraph-checkpoint-sqlite + aiosqlite — async local checkpoints.
- **KEEP AS SECONDARY** Microsoft Agent Framework — migration/interop candidate if measured benefits justify it.
- **OPTIONAL** Deep Agents — higher-level agent patterns only where it removes proven glue without taking ownership of product policy.
- **REUSE SELECTIVELY** Tenacity (Apache-2.0) for adapter/network retry mechanics when richer backoff/coroutine policy is useful; Nika still owns the safety decision about whether an external effect is retryable.
- **CUSTOM (thin)** RuntimeSessionStore, IdempotencyLedger, RuntimeRecoveryService and normalized events because the external runtime cannot know Nika task state/approval/side-effect truth.

### M3 — memory, scheduler, resource control
- **REUSE** APScheduler 3.x for trigger/calendar/interval scheduling; do not write a scheduling engine. Keep a Nika `SchedulerPort` and persisted job mapping.
- **REUSE** psutil (BSD-3-Clause) for process/system CPU, memory, disk/network information and process management/resource observation on Windows.
- **OPTIONAL / ADAPT** Qdrant Python client (Apache-2.0) for semantic/vector memory only. Its local mode can run without a server and later switch to service mode. It must never become authoritative task/runtime state.
- **REUSE** SQLite for authoritative episodic/task/workspace metadata, retention and scheduler mappings.
- **CUSTOM (thin)** memory scopes, consent/retention rules, resource budgets, fairness and Nika job identity because these are product policy.

### M4 — Model Gateway, tools and MCP
- **ADAPT** LiteLLM for broad provider normalization where its exact adopted package/license surface remains compatible; keep it optional and behind Nika provider contracts.
- **REUSE** HTTPX for direct OpenAI-compatible/local HTTP adapters and deterministic timeout/cancellation control.
- **CUSTOM (thin)** direct Ollama adapter for the local endpoint because it is small, predictable and avoids making LiteLLM mandatory for local-only use.
- **REUSE** official Model Context Protocol Python SDK (MIT) for MCP client/server protocol rather than writing MCP framing.
- **REUSE** provider SDKs only where they provide needed streaming/auth/tool features that generic HTTP cannot safely cover.
- **CUSTOM (thin)** model/provider registry, routing/fallback policy, normalized usage/cost/latency/audit records and tool risk policy.

### M5 — accessible Windows UI
- **ADAPT** pywebview + EdgeChromium/WebView2 for the Windows host.
- **REUSE/ADAPT** React + TypeScript + Vite for the local frontend build, subject to packaging proof.
- **REUSE** React Aria Components / React Spectrum accessibility primitives (Apache-2.0) where they measurably improve keyboard/focus/name semantics.
- **REUSE** existing Accessible-Chess WebView2 accessibility-host lessons rather than rediscovering renderer/host behavior.
- **CUSTOM (thin)** Nika screens, accessible status model, Action Registry/Keymap integration, focus restoration and error/report surfaces.
- **HUMAN GATE** actual NVDA acceptance can never be delegated to an automated library.

### M6 — Agent Builder and permissions
- **REUSE** Pydantic schemas/validators for deterministic agent definitions.
- **ADAPT** LLM structured-output facilities only for drafting a proposed definition, never as the source of permission truth.
- **REUSE** JSON Schema export from Pydantic where external tooling needs schemas; do not add a second schema framework without need.
- **CUSTOM (thin)** permission compiler, R0–R4 risk mapping, activation/versioning and human review because these are Nika product policy.

### M7 — multi-agent laboratory
- **REUSE/ADAPT** LangGraph graph/subgraph and durable execution primitives for supervisor/worker orchestration.
- **REFERENCE/OPTIONAL** Agent Zero, CrewAI or Agno only to borrow proven interaction patterns or a narrowly useful adapter; do not run several competing orchestration kernels in production.
- **CUSTOM (thin)** typed Nika handoffs, parent/child durable identity, privilege attenuation, spawn depth/concurrency quotas and cross-agent audit evidence.

### M8 — controlled learning and experiments
- **REUSE** scikit-learn for classical classification/regression/clustering/ranking where data and metrics justify it.
- **ADAPT** DSPy (MIT) only for metric-backed prompt/program optimization with explicit train/eval sets.
- **REUSE** Gymnasium for controlled reinforcement-learning/simulation environment interfaces.
- **REUSE** pandas/numpy only when data workloads justify them; do not make them mandatory core dependencies prematurely.
- **CUSTOM (thin)** experiment/run/metric/dataset/strategy records, champion/challenger promotion, rollback and “never silently rewrite production source” policy.

### M9 — plugin SDK and real workspaces
- **REUSE** Python entry points for installed plugin discovery.
- **REUSE** MCP SDK for externally hosted tools/services.
- **ADAPT** OpenHands SDK/agent-server as first Software Factory coding-worker candidate; keep Nika ownership of project state, branch/test/security/release gates. Exact adopted OpenHands surface must remain permissively licensed.
- **ADAPT** Microsoft UFO² as first Windows Computer Interaction worker candidate behind permission/evidence ports.
- **REUSE** Playwright as deterministic semantic browser baseline (Apache-2.0).
- **ADAPT OPTIONAL** Browser Use only if a proof shows meaningful benefit beyond Playwright while keeping its broad dependency surface optional.
- **CUSTOM (thin)** plugin/workspace manifest/version/capability/permission compatibility layer.

### M10 — security, sandbox, reliability
- **REUSE** Python keyring for OS-backed credential storage; on Windows use the supported Windows credential backend rather than plaintext config.
- **REUSE** detect-secrets for codebase secret prevention; complement it with repository-host secret scanning when enabled.
- **REUSE** pip-audit for known Python dependency vulnerabilities.
- **REUSE** psutil for process observation/termination evidence where appropriate.
- **REUSE/ADAPT** OS/process isolation primitives and disposable worker environments rather than inventing a security boundary inside normal Python objects.
- **REUSE SELECTIVELY** watchdog (Apache-2.0) for filesystem change events where a workspace needs watch-mode operation; do not poll directories unnecessarily.
- **REUSE SELECTIVELY** structlog (MIT/Apache-2.0 dual license) for structured JSON/event logging if the standard logging adapter becomes insufficient.
- **CUSTOM (thin)** R0–R4 policy, approval ledger, sandbox policy, secret-reference semantics, backup/restore manifests and fail-closed recovery.

### M11 — Windows packaging and distribution
- **ADAPT** PyInstaller as first freezing path; verify licenses/notices and WebView2 behavior in the actual bundle.
- **KEEP AS FALLBACK** Nuitka if PyInstaller has measured startup, native-dependency or packaging blockers.
- **REUSE** Microsoft Edge WebView2 Runtime already present/redistributable through supported Microsoft deployment path rather than bundling a browser engine arbitrarily.
- **CUSTOM (thin)** Nika release manifest, optional-component downloader/manager, checksums, migration/backup hooks and offline packaging policy.
- Heavy workers/models stay optional: no Ollama models, speech models, OCR/vision models or coding sandboxes in the base EXE unless a release explicitly requires them.

### M12 — full QA, accessibility and release
- **REUSE** pytest/pytest-cov/Ruff/pip-audit/detect-secrets plus GitHub Actions Windows+Ubuntu jobs.
- **REUSE/ADAPT** Playwright tests for semantic UI/web flows where applicable.
- **REUSE** accessibility tooling for automated checks as a supplement only.
- **CUSTOM (thin)** recovery drill, release acceptance matrix, product-specific E2E fixtures and human NVDA protocol.
- **HUMAN ONLY** final NVDA VERIFIED status.

## Cross-cutting ready engines

### Windows “eyes/hands”
1. **Microsoft UFO²** — first full Windows computer-use proof.
2. **pywinauto** — lower-level direct Windows UI Automation/Win32 fallback.
3. **UI-TARS Desktop / OmniParser** — optional screenshot/vision grounding candidates only after exact model/component license audit.
Policy: UI Automation/semantic control first; screenshots/OCR/coordinates are fallback.

### Browser “eyes/hands”
1. **Playwright** — first deterministic DOM/role/label/text controller.
2. **Browser Use** — optional higher-level agent worker behind Nika browser port if it wins a proof.
Policy: do not make vision clicking the default when DOM/accessibility semantics exist.

### Coding / Software Factory
1. **OpenHands** — first coding-worker proof.
2. External coding/model systems may be adapters through their supported API/SDK/CLI.
3. Nika owns sandbox/worktree/branch/test/security/integration/release policy.

### Speech and audio
- **sherpa-onnx** (Apache-2.0) — strong optional offline bundle candidate covering speech recognition, TTS, VAD, keyword spotting and related speech tasks across Windows and other platforms.
- **faster-whisper** (MIT) — optional Whisper transcription worker where Whisper compatibility/accuracy is the key requirement.
- Keep models outside base installation; choose per task/benchmark.

### OCR / document vision
- **Tesseract OCR** (Apache-2.0) — lightweight mature OCR fallback.
- **PaddleOCR** (Apache-2.0) — heavier multilingual OCR/document parsing option.
- **OpenCV** (Apache-2.0) — preprocessing/vision primitives, not a general reasoning engine.
- Vision-language models are optional model workers through Model Gateway, never mandatory kernel dependencies.

### Offline “minimal brain”
- **Unified Planning** (Apache-2.0) for explicit symbolic planning domains.
- **scikit-learn** for measured classical ML tasks.
- **ONNX Runtime** (MIT) for compact specialist models.
- **OpenCV** for deterministic image-processing pipelines.
- **Gymnasium** for controlled simulation/RL environments.
No combination of these is falsely presented as a general GPT/Claude replacement.

## Rejected / cautionary adoption patterns
- Do not wholesale-copy large repositories into Nika Core.
- Do not let third-party runtime/framework classes become Nika domain contracts.
- Do not run LangGraph + Agent Framework + CrewAI + Agno as parallel production kernels without a measured requirement.
- Do not make Docker/Linux-heavy workers part of the Windows/NVDA UI process; isolate them as optional backend workers.
- Do not use vector stores as authoritative transactional product state.
- Do not store secrets in `.env` inside the repository; `.env` is local-only and ignored. Prefer OS credential storage for persistent secrets.
- Do not adopt GPL/restrictive/mixed-license components into the distributed base without an explicit legal/architecture decision. In particular, newer Piper variants and modern AutoGPT/platform surfaces require separate license review.
- Do not adopt an upstream package solely because it is popular; require a concrete Nika capability, maintenance check, license fit and proof test.

## Implementation order under PARALLEL-FIRST
This catalog applies to all M3–M12 lanes simultaneously. Dependencies constrain merge/integration, not isolated proofs. Each lane must perform a fresh upstream/version/license check immediately before adding a dependency because upstream projects change.

The expected result is a small Nika-specific control plane around proven components, not a giant custom reimplementation of browsers, schedulers, coding agents, OCR, speech, model gateways, vector search, retry engines, resource monitors, packaging systems or accessibility primitives.
