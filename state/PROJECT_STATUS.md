# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable CI is green.
- M2 durable runtime package is IMPLEMENTED/PREPARED on `dev/m2-runtime-selection` / PR #3 but not INTEGRATED; its 11% weight is not credited until real framework tests execute and are green.

## Current milestone
M1 integration remains externally blocked by GitHub Actions account billing/spending runner allocation. Safe dependent M2 work may continue, but no unchecked M3+ functional backlog is allowed. While the runner blocker persists, cycles prioritize M1/M2 source review, testability, documentation and reuse research.

## M1 candidate
PR #2 head: `9f73aa4b4a560bd66410295ccc75303e1a037e70`.
Includes typed/versioned configuration, persisted Agent/Workspace registries, Audit Log, workspace discovery contract, central Action Registry and persisted remappable Keymap. M2 extends the database migration chain without changing the M1 product contract.

## M2 current branch
PR #3: `dev/m2-runtime-selection` -> `dev/m1-foundation`.
Current source head before this status commit: `1c2e13515eb0a8b3c3779f439aa1131afcce95dc`.
PR remains intentionally dependent on M1 and must not be merged to main before PR #2 is independently green and integrated.

## M2 implemented/prepared capabilities
- LangGraph selected as primary orchestration runtime behind framework-neutral `AgentRuntimePort`; Microsoft Agent Framework remains secondary adapter/migration candidate.
- Async local durability uses `langgraph-checkpoint-sqlite` `AsyncSqliteSaver` + `aiosqlite`; strict MsgPack checkpoint deserialization is forced.
- Real LangGraph/SQLite proof suites are prepared for restart without repeated completed side effects, approval interruption across recreation, corrupt-checkpoint fail-closed behavior, real Nika coordinator persistence mapping and bounded active cancellation.
- Active invocations are tracked by exact `(task_id, thread_id)` and duplicate concurrent execution is rejected.
- Runtime requests support positive wall-clock deadlines, typed failures and fail-closed explicit retry policy with bounded backoff.
- `RuntimeSessionStore` durably maps Nika task -> runtime/thread/resume token and prebinds an ACTIVE pointer before durable execution so abrupt process loss does not orphan checkpoints from the Nika task.
- `IdempotencyLedger` provides framework-neutral stable operation keys, input fingerprints and fail-closed reconciliation for external side effects.
- `RuntimeRecoveryService` inventories persisted sessions after process recreation and separates safe crash continuation from approval/manual/reconciliation/error cases.

## Current cycle — reuse-first digital worker architecture
No GitHub Actions rerun was intentionally triggered in this cycle because the same account-level runner allocation blocker had already been reprobed recently; canonical policy allows at most one equivalent infrastructure probe roughly every six hours unless account/configuration state changes.

Instead, this cycle performed a fresh official-source reuse audit so future Agent Lab work does not recreate mature external systems.

### Computer interaction decisions
New canonical document: `docs/COMPUTER_INTERACTION_REUSE_AUDIT.md`.

- ADAPT Microsoft UFO² as the first Windows computer-use proof candidate rather than designing a complete Windows AgentOS from scratch. Its current architecture already combines Windows UI Automation, native/application APIs, visual fallback, hierarchical agents and MCP action servers.
- REUSE Playwright as the deterministic browser automation baseline, prioritizing role/label/user-visible accessibility semantics and strict target resolution.
- ADAPT Browser Use only as an optional higher-level browser-agent layer if a future proof shows measurable value over raw Playwright. Keep its broad dependency/provider surface out of mandatory Nika Core.
- KEEP a smaller direct Windows UIA/pywinauto-style adapter as fallback if UFO² is too heavy or cannot be isolated safely.
- Vision/OCR/coordinate actions remain fallback after structured API/DOM/UIA methods.
- Future Nika Computer Interaction contracts remain capability-oriented; third-party framework classes cannot leak into Agent Lab domain APIs.

### Software Factory decisions
New canonical document: `docs/SOFTWARE_FACTORY_AND_OFFLINE_INTELLIGENCE_REUSE.md`.

- ADAPT OpenHands Software Agent SDK/agent-server as the first coding-worker proof candidate instead of rebuilding a complete coding-agent shell/editor/tool runtime. Only permissively licensed core/SDK surfaces are default candidates; separately licensed enterprise components are excluded unless explicitly approved later.
- Nika remains owner of repository/workspace identity, branch/worktree isolation, permissions, acceptance gates, audit and release truth. A coding worker returns patches/commits/test evidence and never writes production main directly.
- Future `CodingWorkerPort` keeps OpenHands replaceable.

### Offline/minimal-intelligence decisions
- ADAPT Unified Planning behind a future deterministic planner port for domains with explicit states/actions/preconditions/effects.
- REUSE ONNX Runtime for compact specialist inference only when an actual trained model and metric justify it; ONNX Runtime is not treated as a general reasoning brain.
- REUSE classical ML per measured task rather than adding a generic mandatory ML bundle.
- No-LLM mode remains deterministic/specialized autonomy, not falsely advertised GPT-level reasoning.

### Master baseline update
`docs/MASTER_SPEC.md` was advanced to v1.4 and now makes the digital-worker Computer Interaction Layer, Accessibility Repair Agent, Software Factory and offline/minimal-intelligence boundaries explicit. `docs/THIRD_PARTY_ADOPTION.md` records all new REUSE/ADAPT decisions and the rule that these future adapters are not implemented until M1/M2 executable integration is restored.

## Source evidence checked this cycle
Official upstream material checked on 2026-08-18:
- Microsoft UFO² architecture and MIT license;
- Playwright Python locator/accessibility guidance;
- Browser Use repository/license/current dependency surface;
- OpenHands Software Agent SDK and licensing boundary;
- Unified Planning stable docs/project;
- ONNX Runtime Python inference API.

## Infrastructure blocker
Most recent canonical M1 evidence remains a GitHub Actions job that failed before checkout/dependency/test steps (`steps = null`), with previously captured GitHub annotation identifying account payment failure or Actions spending-limit configuration. This is infrastructure evidence, not code-test evidence.

No new rerun was spent in this cycle due the six-hour duplicate-blocker probe policy. No new test is claimed PASSED.

## Test truth
- New reuse documents and master/adoption architecture updates are committed.
- M1/M2 executable tests remain unproven in hosted CI.
- No new product percentage is credited for architecture/research documentation.

## Truth state
- M0: INTEGRATED / green CI.
- M1: IMPLEMENTED, not INTEGRATED, not PACKAGED, not HUMAN_TESTED.
- M2: IMPLEMENTED/PREPARED across durable runtime, recovery, cancellation, timeout/retry and side-effect safety; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- Digital worker reuse architecture: RESEARCHED/DOCUMENTED, not IMPLEMENTED.

## Packaging policy
No EXE in this cycle. Build Windows standalone only at milestone/user-test/release gates. Heavy coding/browser/vision/model workers should remain separable optional components instead of inflating mandatory Nika Core.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Respect the duplicate infrastructure-probe interval; re-check Actions only when the interval/configuration warrants it.
2. As soon as runners execute: run/fix/merge PR #2 only if M1 Ruff/compile/pytest are genuinely green.
3. Retarget/rebase PR #3 onto green main, execute `.[dev,agent]` Ruff/compile/pytest and fix all real API/runtime/migration failures.
4. Execute the full real LangGraph/SQLite durability suite together: startup recovery, pre-result process loss, no-repeat completed work, approval recreation, corrupt checkpoint fail-closed, cancellation, timeout/retry and persisted sessions.
5. Only after M2 is executable-green begin M3 as one coherent implementation package.
6. When the roadmap reaches Computer Interaction/Software Factory implementation, run bounded proof branches for Playwright, UFO² and OpenHands before accepting them as dependencies.