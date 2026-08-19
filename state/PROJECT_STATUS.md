# PROJECT STATUS — Nika Core

Updated: 2026-08-19.
Canonical repository: `Oleksii-debug/Nika-Core`.
Repository visibility observed: PUBLIC.
Development mode: **ACTIVE DEVELOPMENT — Product Journey repair + Full Product Vision expansion**.

## Practical truth first

The previously recorded M12 Windows artifact is **not a valid human NVDA candidate anymore**. A concrete packaged-product defect was found: user-visible task controls/lists were not fully wired to the real backend. A large repair exists in PR #37, but its latest exact combined head has Core CI and Windows release-candidate success while the full M12 gate was cancelled. Therefore it is not yet eligible for integration/human-candidate promotion.

At the same time, the technical-project reconciliation expanded the binding end-state scope. The old “98% A–Z” number is retained only as **historical Core milestone credit**. It is not a Full Product Vision completion percentage.

Current human truth:
- HUMAN_TESTED: **false**;
- NVDA_VERIFIED: **false**;
- PRODUCTION_RELEASE_READY: **false**;
- valid current human NVDA candidate: **none**.

## Historical Core foundation evidence

The original M0–M11 foundations and an earlier automated pre-human M12 candidate did receive exact green evidence for their then-scoped acceptance gates. This includes durable runtime/recovery, memory/scheduler/resources, ModelGateway/Ollama/cloud foundation, tools/MCP, accessible WebView2 shell, Agent Builder, multi-agent execution, Experiment Engine, plugin/workspace/security foundations and Windows packaging.

Historical scoped Core progress before the newly discovered Product Journey defect was recorded as **98%**. That number is archival evidence about the original roadmap, not a statement that the expanded final Nika product is 98% complete.

Detailed historical SHA/run/artifact evidence remains in Git history and LIVE DASHBOARD Issue #1; do not copy it forward as current candidate truth when newer live evidence conflicts.

## Current canonical baseline

Starting `main` for the current intelligence reconciliation lane: `8065cc3fedb63f9c07e1773acf2332b5709560da`.

That main includes the Windows release third-party notice/license repair. It does **not** yet include the open functional Windows backend repair or the open embedded/deterministic intelligence lane.

## Active lane A — Windows Product Journey repair

PR #37: `fix/windows-desktop-functional-backend`.
Latest inspected exact head: `a56a9193fd9e7ae30ae8acac997609f38db6fef9`.

Implemented scope:
- real task create path through Nika backend/runtime;
- real persisted task/agent/workspace state exposed to the UI;
- pause/resume/stop wiring;
- replacement of placeholder lists with backend state;
- deterministic backend/bridge lifecycle tests;
- compatibility merge with the already-integrated release-license/notices repair.

Latest exact-head evidence inspected during this development wave:
- Core CI #228: SUCCESS;
- M11 Windows Release Candidate #8: SUCCESS;
- M12 Pre-Human Release Gate #6: CANCELLED.

Conclusion: implementation is substantial and partially green, but the complete required release gate is not green. **Do not promote a ZIP from this lane without newer exact-head full-system evidence.**

## Active lane B — Deterministic Brain + Embedded Brain

PR #40: `feat/embedded-intelligence-foundry-local`.
Lane started independently from the recorded `main` baseline and does not overlap PR #37's eight product/UI/test files.

Implemented in the lane:
- `FoundryLocalProvider` behind the existing ModelGateway contract using the official Microsoft Foundry Local Python SDK path;
- model download defaults to disabled/fail-closed so ordinary inference cannot silently download large model files;
- optional Windows `foundry-local-sdk-winml` dependency plus cross-platform SDK alternative;
- a first-class model-free `DeterministicBrain` with Nika-owned explicit world-state/goal/action contracts;
- Unified Planning/Pyperplan adapter behind the Nika planner contract;
- deterministic plans execute through existing guarded ToolExecutor, preserving approval boundaries;
- deterministic planning has explicit wall-time and maximum-step budgets; an oversized plan is rejected before executing a tool;
- ModelGateway has explicit ordered fallback provider IDs, a single total deadline, privacy prevalidation of the complete route, and fallback only after eligible retryable failures;
- provider capabilities record whether hard cancellation is proven. A timeout from a provider without proven hard cancellation is non-retryable and cannot trigger concurrent fallback;
- Foundry Local in-process inference is serialized. If an async timeout/cancellation occurs while native non-streaming inference continues, the Foundry slot remains reserved until that worker actually exits;
- Foundry model metadata can be inspected read-only for exact model/cache/hardware evidence without silently loading or downloading the model;
- `scripts/prove_foundry_local.py` prepares a physical-Windows evidence record with SDK version, platform, model identity, explicit human-reviewed model-license reference, inference result and optional deterministic model-cache tree checksum. Model download remains opt-in only;
- deterministic tests cover no-model multi-step execution, impossible goal, re-planning, high-impact approval denial, plan/time budgets, explicit fallback safety, sensitive-route rejection, non-cancellable timeout behavior, Foundry serialization, timeout/native-worker slot retention, model metadata and missing-model errors;
- Windows CI contains a dependency/import proof for the official Foundry Local SDK package;
- master/full-product/reuse/acceptance documents are reconciled to the expanded intelligence architecture.

Evidence boundary: this lane is **IMPLEMENTED but requires fresh exact-head CI after the latest coherent changes before it may be called GREEN or INTEGRATED**. Earlier green runs on older PR #40 heads do not prove the latest head.

A real physical-Windows Foundry model inference has **not** been executed by automation here. SDK import, fake-manager tests and the physical-proof script are preparation/evidence infrastructure only. The exact model license remains a per-model human/release review item rather than being inferred from the MIT SDK license.

## Expanded Full Product Vision

Binding documents:
- `docs/FULL_PRODUCT_VISION_2026-08-19.md`;
- `docs/INTELLIGENCE_REUSE_2026-08-19.md`;
- `docs/WORKSPACE_REUSE_CATALOG_2026-08-19.md`;
- updated `docs/MASTER_SPEC.md`, `docs/ROADMAP.md` and `docs/ACCEPTANCE_GATES.md`.

New/clarified end-state capability groups include:
1. Deterministic Brain with no model at all;
2. Embedded Brain with Microsoft Foundry Local primary and measured llama.cpp/ONNX Runtime GenAI alternatives;
3. external local models such as Ollama;
4. optional cloud/API models;
5. Capability Escalation / Toolsmith that can safely obtain a missing tool and resume the original task;
6. Product Journey gate from packaged accessible UI to real persisted backend and recovery;
7. Universal Research Engine + reusable Corpus/Knowledge layer;
8. Model Engineering Lab;
9. real AI Trader workspace rather than merely generic experiment infrastructure;
10. resource/power-aware local operation and shared accessible reports.

Telegram is explicitly removed from active roadmap/workspace scope. Historical Telegram references are archival only.

## Progress accounting

Do not publish a new invented Full Product Vision percentage yet. First define/close the expanded capability gates and, if desired later, assign explicit weights.

Report instead:
- which practical product journeys are integrated and green;
- which are implemented but not integrated;
- whether a current Windows candidate exists;
- what remains human-only.

## Current blockers

1. PR #37 requires complete current exact-head full-system evidence before integration/promotion if no newer live result supersedes the recorded cancelled M12 run.
2. PR #40 requires fresh exact-head Ubuntu+Windows Core CI, the Foundry SDK dependency proof and applicable M12 full-system gate after the latest timeout/fallback/evidence changes.
3. Even after Foundry adapter integration, a real physical-Windows embedded-model inference with exact model/version/license/resource evidence is still required before describing Foundry Local as hardware-proven.
4. HUMAN_TESTED/NVDA_VERIFIED remain human-only.

## Next large coherent batches

- finish exact-head CI and integration for the independent deterministic/embedded intelligence lane;
- finish/re-run the complete Product Journey repair gate and integrate only if fully green/current-main-compatible;
- after both are safely integrated, produce one fresh combined Windows candidate rather than promoting an intermediate ZIP;
- then allow reserved manual lanes to advance Toolsmith/Capability Escalation, Universal Research/Corpus and other Full Product Vision work without AUTO02 source overlap.
