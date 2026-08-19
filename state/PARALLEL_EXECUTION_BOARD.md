# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-19.
Mode: **ACTIVE DEVELOPMENT — Product Journey repair + Full Product Vision expansion**.
Canonical evidence: live GitHub code + exact executable checks + integration. Historical artifacts never override newer defect evidence.

## Evidence states
- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — exact branch/PR head passed required automated checks.
- INTEGRATED — exact green candidate merged into `main`.
- PACKAGED — installable Windows artifact built and checked.
- HUMAN_TESTED — a person completed the specified manual protocol.
- NVDA_VERIFIED — the human NVDA protocol passed; automation may never award this state.

## Current product truth

The old **98%** number is retained only as historical credit for the original scoped Core roadmap. It is **not** a Full Product Vision completion percentage.

Current practical truth:
- the reusable Core foundation is substantial and historically well-tested;
- a concrete Product Journey defect invalidated the old Windows package as a current human candidate;
- no Windows ZIP is currently approved for the user's NVDA test;
- HUMAN_TESTED: false;
- NVDA_VERIFIED: false;
- PRODUCTION_RELEASE_READY: false;
- Full Product Vision percentage: deliberately **not assigned yet**.

Starting `main` for current independent lanes: `8065cc3fedb63f9c07e1773acf2332b5709560da`.

## Active lane ownership

### L-WIN — Real Windows Product Journey
Owner scope: final Windows UI/backend lifecycle only.
PR #37 — `fix/windows-desktop-functional-backend`.
Latest inspected head: `a56a9193fd9e7ae30ae8acac997609f38db6fef9`.

Implemented:
- real task creation through Nika backend/runtime;
- real persisted Tasks/Agents/Workspaces state in the UI;
- pause/resume/stop wiring;
- removal of placeholder-only state surfaces;
- backend/bridge lifecycle tests;
- compatibility with already-integrated Windows release license/notices repair.

Evidence inspected:
- Core CI: SUCCESS;
- Windows release-candidate build: SUCCESS;
- complete M12 pre-human gate: CANCELLED.

State: **IMPLEMENTED / PARTIALLY GREEN / NOT INTEGRATED**.
Blocker: complete non-cancelled full-system gate on the exact final head. Do not promote an intermediate ZIP.

Owned files currently include the Windows launcher, task/agent registry lifecycle surfaces, DesktopBackend/bridge/web UI and its dedicated tests. Other lanes must not collide with those files without an explicit compatibility decision.

### L-INT — Deterministic Brain + Embedded Brain
Owner scope: model-free planning/execution, embedded-model adapter architecture, intelligence reuse contracts and directly coupled tests/docs.
PR #40 — `feat/embedded-intelligence-foundry-local`.

Implemented on branch:
- first-class **Deterministic Brain** that uses no language model;
- Nika-owned world-state/goal/action/plan contracts;
- Unified Planning/Pyperplan adapter for explicit planning domains;
- execution through existing guarded ToolExecutor so planner-selected actions preserve approval/security rules;
- Microsoft **Foundry Local** in-process provider behind existing ModelGateway;
- explicit/fail-closed model download policy;
- optional Windows WinML SDK dependency and separate cross-platform SDK path;
- fake-SDK Foundry provider contract tests without downloading a large model;
- deterministic planning/execution/impossible-goal/replanning/high-impact-approval tests;
- Windows CI proof that the selected official Foundry SDK package resolves/imports;
- expanded Full Product Vision, current intelligence reuse decisions, Product Journey/Toolsmith gates and current scope docs.

State: **IMPLEMENTED / FINAL CI REQUIRED / NOT INTEGRATED** until the latest exact head completes all required checks.
Remaining after code-level integration: real physical-Windows Foundry model inference benchmark/proof before calling the embedded model hardware-proven.

Collision decision: this lane was created from the same current `main` and intentionally does not modify PR #37's eight owned product/UI files, so both lanes may advance in parallel.

## Prepared next Full Product Vision lanes

These lanes are real end-state product work. They do not receive progress credit merely because architecture is documented.

### L-TOOL — Capability Escalation / Toolsmith
Goal: active task proves a missing capability -> reuse search -> bounded Software Factory/CodingWorker request if necessary -> isolated implementation/test -> permission-aware registration -> original task resumes from checkpoint.
Prerequisites: stable current Software Factory/CodingWorker boundary and task/checkpoint contracts.
Must not self-modify production main or self-expand permissions.

### L-RESEARCH — Universal Research + Corpus/Knowledge
Goal: one reusable engine for sources, incremental freshness/change state, HTTP/API/browser/document extraction, deterministic pre-filter, evidence/confidence, dedup, structured cards, review, scheduled deltas and accessible reports; plus provenance-scoped local corpus/FTS before optional semantic retrieval.
GrantScanner/Product Search/etc. become profiles rather than duplicate crawlers.

### L-MODEL — Model Engineering Lab + local resource profiles
Goal: benchmark Foundry/Ollama/cloud/specialist models on versioned tasks; measure quality/latency/RAM/CPU/GPU; manage model license/checksum/artifacts; add normal/battery/night/low-memory/heavy-worker resource profiles where measured.

### L-TRADER — AI Trader real workspace
Goal: no-lookahead replay, chronological odds/event snapshots, virtual bank, singles/combinations/portfolio exposure, time waves, versioned strategies, risk/drawdown, held-out validation, paper/live-prematch simulation, restart-safe sessions and accessible reports.
Generic M8 Experiment Engine is foundation only; it does not mean AI Trader itself exists.

### L-CMD — Command center + workspace creation Product Journeys
Goal: user can give a natural-language command in final accessible Windows UI, create/configure agents/workspaces/tasks, route through real services, see durable accessible results, and request a missing capability through Toolsmith.

### L-RELEASE — Combined release/integration
Goal: after required product lanes merge, rebuild one exact Windows candidate containing all required repairs; run Core, security/recovery, Product Journey, WebView2/UIA/keyboard/focus/package/license/checksum gates; only then hand the exact artifact to the human NVDA protocol.

## Removed active scope

**Telegram is removed from active Nika Core development.** Historical Telegram/Telethon/TDLib material is archival reference only. Scheduled/manual workers must not spend development time on Telegram unless the user explicitly introduces it as a new future workspace.

## Parallel collaboration model

### Scheduled workers — normal mode
Five scheduled workers may run simultaneously when manual Deep Research coding lanes are not active. They should own large non-overlapping vertical areas rather than microtasks.

### Manual Deep Research developer/auditor mode
When the user starts manual Deep Research developer chats, those chats are **real coding lanes**, not research-only roles. Each may take a large subsystem, read live GitHub, implement code, trigger tests and drive one coherent branch toward integration. A paired auditor independently inspects live code/evidence and returns defects.

During manual Deep Research coding periods, scheduled workers should be **paused or reassigned** to complementary low-collision duties such as:
- cross-lane Integration QA;
- release/package proof;
- regression/security/accessibility auditing;
- dependency/license/evidence consistency;
- stale-conflict detection and merge readiness.

They should not simultaneously edit the same subsystem just to maximize agent count.

## Collision policy
1. Separate branch for each independent lane.
2. Branch from latest compatible green `main` unless a real dependency requires otherwise.
3. Do not stack unrelated branches.
4. Shared-contract edits require explicit compatibility decision and targeted tests.
5. If another lane owns overlapping files, audit/research another independent surface rather than create competing edits.
6. A blocked lane does not block independent lanes.
7. Acceptance credit requires exact green evidence + integration, not implementation alone.

## Large-batch policy
A development run is not one file/function/test. Each lane should advance the largest safe coherent slice: reuse check -> contract -> implementation -> persistence/recovery -> errors -> tests -> accessibility/security impact -> docs/evidence -> CI/integration. Stop only at a real subsystem boundary, required external proof, blocker or safety approval boundary.

## CI/release policy
- Shared verification runs on Ubuntu + Windows where applicable.
- Focused WebView2/UIA/package/security/model-hardware proofs run only when they add real evidence.
- Never weaken a gate to get green.
- Do not repeatedly download large models or rebuild release EXEs on every development push.
- An integrated behavior change invalidates any older human-candidate artifact until a fresh combined exact candidate passes the complete release gate.

## Human truth
- HUMAN_TESTED remains false until the user actually performs the exact protocol.
- NVDA_VERIFIED remains false until the user actually completes the Windows/NVDA acceptance.
- Automated accessibility evidence never promotes either state.
