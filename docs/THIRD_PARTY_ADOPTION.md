# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, inspect current official documentation and maintained upstream projects. Record REUSE, ADAPT or CUSTOM. Prefer package dependencies/adapters over vendored source.

## M1 decisions — 2026-08-18
- REUSE — Pydantic + Pydantic Settings for typed/versioned configuration and `NIKA_*` environment loading.
- REUSE — Python `sqlite3` for the local deterministic Nika store and transactional writes.
- CUSTOM (thin) — ordered SQLite schema migrations. At the current small local-only schema, Alembic would add SQLAlchemy/migration complexity without a corresponding benefit. Re-evaluate Alembic when schema transforms become complex or another SQL backend is introduced.
- REUSE — Python `importlib.metadata.entry_points()` as the installed-workspace discovery mechanism. Nika owns only the stable workspace contract and entry-point group.
- CUSTOM — Agent/Workspace registries, Action Registry/Keymap and Audit Log because they encode Nika-specific versioning, accessibility, safety and product policy.

## M2 runtime decision — 2026-08-18
- ADAPT — LangGraph as the primary durable orchestration runtime behind Nika `AgentRuntimePort`.
- REUSE — `langgraph-checkpoint-sqlite` with `AsyncSqliteSaver` for the first local durable checkpoint adapter/proof because Nika's runtime contract invokes graphs asynchronously.
- REUSE — `aiosqlite` as the SQLite driver required by the official LangGraph async SQLite saver.
- REUSE — Python `asyncio` task cancellation and `wait_for()` for bounded in-process cancellation/deadlines at the Nika adapter boundary; no extra retry/timeout framework is needed.
- SECURITY ADAPTATION — force strict MsgPack deserialization at the Nika checkpointer construction boundary. This follows current upstream security guidance and is not user-disableable.
- KEEP AS SECONDARY CANDIDATE — Microsoft Agent Framework. Its Python core/workflow surface is strong, but the current local SQLite path is less direct for Nika's first desktop durability proof.
- CUSTOM (thin) — Nika runtime contracts, normalized events/results, capability registry, task/audit coordinator and selection boundary. These prevent any framework type from becoming a Nika domain dependency.
- CUSTOM (thin) — Nika `RetryPolicy`. Retry decisions encode Nika side-effect/idempotency safety and audit semantics, so they must remain framework-neutral. Retries are disabled by default, opt into exact typed failure classes, and require a durable resume token unless an explicit caller accepts fresh replay risk.
- CUSTOM (thin) — Nika `RuntimeSessionStore`. LangGraph owns checkpoint bytes and thread state, while Nika must durably map its product `task_id` to the selected runtime, `thread_id` and opaque resume token so UI/workspace code can recover after process recreation without leaking framework identifiers.
- CUSTOM (thin) — Nika `IdempotencyLedger`. Providers differ in native idempotency/reconciliation semantics, so Nika needs one framework-neutral fail-closed record for stable operation keys, input fingerprints and UNCERTAIN outcomes. Provider-native idempotency remains preferred and adapters must reconcile ambiguous external outcomes rather than blindly replaying them.
- REUSE/ADAPT — keep LangGraph checkpoint/thread persistence as the source of graph execution truth during restart; use the same durable `thread_id` cursor rather than duplicating framework checkpoint bytes.
- CUSTOM (thin) — Nika `RuntimeRecoveryService`. LangGraph cannot know Nika task states, registered runtime availability, approval policy or Nika's external side-effect ledger. Startup recovery therefore inventories Nika-owned facts and only auto-resumes clean crash-left ACTIVE/RUNNING sessions. PENDING/UNCERTAIN external actions, approval waits, missing runtimes and state mismatches fail closed.

The dated comparison and executable proof design are in `docs/RUNTIME_SELECTION.md`. Restart/session and side-effect policy is in `docs/RUNTIME_RECOVERY_AND_SIDE_EFFECTS.md`; startup-wide recovery classification is in `docs/STARTUP_RECOVERY.md`. Do not run several orchestration kernels in production simultaneously without measured benefit. Re-run the selection gate if upstream stability, persistence or provider support materially changes.

Deep Agents, LiteLLM, MCP Python SDK, APScheduler and DSPy remain candidates for their planned milestones and must be re-verified immediately before adoption.

## Digital-worker / Computer Interaction audit — 2026-08-18
Fresh official upstream audit is recorded in `docs/COMPUTER_INTERACTION_REUSE_AUDIT.md`.

- ADAPT — Microsoft UFO² as the first Windows computer-use proof candidate. It already combines Windows UI Automation, application introspection, native APIs, visual fallback, hierarchical agents and MCP action servers. Do not build a full Windows AgentOS before proving whether UFO² can sit behind Nika's permission/audit contracts.
- REUSE — Playwright as the deterministic browser control baseline. Prefer role/label/text/accessibility semantics and strict locators before visual/coordinate control.
- ADAPT (optional) — Browser Use as a higher-level browser-agent adapter only if it measurably reduces glue code beyond Playwright. Keep its broad provider/tool dependency surface out of mandatory Nika Core.
- REUSE FALLBACK — direct Windows UI Automation/pywinauto-style adapter if UFO² is too heavy or cannot be isolated safely.
- CUSTOM (thin, future) — Nika capability-oriented Computer Interaction ports and normalized evidence/action results. Third-party UI classes must never leak into Agent Lab domain APIs.
- VISION POLICY — screenshot/OCR/vision grounding is fallback for missing semantics, not the default primary control path.

## Software Factory and offline-intelligence audit — 2026-08-18
Fresh official upstream audit is recorded in `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`.

- ADAPT — OpenHands Software Agent SDK / agent-server as the first coding-worker proof candidate. Use only the permissively licensed core/SDK surfaces unless a separate enterprise-license decision is made. Nika remains owner of project state, branch policy, safety, approvals and release gates.
- ADAPT — Unified Planning as a future deterministic formal-planning candidate behind a Nika planner port for domains with explicit states/actions/preconditions/effects.
- REUSE — ONNX Runtime for compact specialist neural-model inference; it is an inference engine, not a general reasoning brain.
- REUSE PER MEASURED TASK — maintained classical ML libraries such as scikit-learn for classification/regression/clustering/ranking only where datasets and metrics justify the dependency.
- CUSTOM (thin, future) — Nika `CodingWorkerPort` / `DeterministicPlannerPort` contracts so coding/planning implementations remain replaceable.

Do not implement these future adapters while M1/M2 integration is unverified. The current CI blocker period is for source review, testability, documentation and reuse research, not for building an unchecked M3+ backlog.

## Windows UI
ADAPT — pywebview + EdgeChromium/WebView2 with local HTML/CSS/JS. Reuse the Accessible Chess WebView2 accessibility-host lessons. React + TypeScript + Vite + React Aria Components remain the M5 frontend candidate subject to a fresh audit.

## Packaging
ADAPT — PyInstaller is the first pywebview Windows freezing path; Nuitka remains a measured fallback. Do not package every development cycle.

## Mandatory pre-code record
Every new subsystem decision is classified REUSE, ADAPT or CUSTOM. CUSTOM requires a short explanation of why maintained upstream options do not satisfy the requirement.