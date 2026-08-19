# Nika Core autonomous development instructions

This repository is the canonical source of truth. Chat history is not.

Before every coding cycle read: `docs/MASTER_SPEC.md`, `docs/FULL_PRODUCT_VISION_2026-08-19.md`, `docs/ROADMAP.md`, `docs/THIRD_PARTY_ADOPTION.md`, `docs/REUSE_CATALOG_2026-08-18.md`, `docs/INTELLIGENCE_REUSE_2026-08-19.md`, `docs/WORKSPACE_REUSE_CATALOG_2026-08-18.md`, `docs/WORKSPACE_REUSE_CATALOG_2026-08-19.md`, `docs/UI_ARCHITECTURE.md`, `docs/LARGE_BATCH_POLICY.md`, `docs/PARALLEL_DEVELOPMENT_POLICY.md`, `docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md`, `docs/ACCEPTANCE_GATES.md`, `state/PROJECT_STATUS.md`, `state/PARALLEL_EXECUTION_BOARD.md`, LIVE DASHBOARD, open PRs and current CI.

Primary rule: **REUSE BEFORE REWRITE**. Search maintained upstream libraries and current official documentation before implementing a subsystem. Default decision order is **REUSE -> ADAPT -> CUSTOM (thin)**. A CUSTOM decision is invalid unless it records why maintained upstream options do not satisfy the requirement. Do not copy random or wholesale third-party source into this repository when a package dependency/adapter is sufficient. Do not add broad unused dependencies merely “for later”; graduate a candidate through a focused proof, exact license/version check and tests.

Architecture: Windows-first modular monolith with ports/adapters and versioned contracts. Nika owns task/audit/permission/product contracts; provider-neutral Model Gateway; workspace/plugin boundaries; Deterministic Brain contracts; deterministic state/validation/dedup/safety; and the Product Journey gate. Language models are replaceable capabilities, not the platform kernel. Agent orchestration sits behind `AgentRuntimePort`; third-party framework/model/planner types must not leak into Nika domain APIs.

Intelligence truth: Nika has four distinct paths that must not be conflated: (1) model-free Deterministic Brain, (2) embedded local model with Microsoft Foundry Local as primary Windows adapter and measured alternatives such as llama.cpp/ONNX Runtime GenAI, (3) external local model servers such as Ollama, and (4) allowed cloud/API providers. Installing or testing one does not award evidence for the others.

Runtime truth: M2 selected and integrated LangGraph behind `AgentRuntimePort`. Microsoft Agent Framework remains a secondary migration/interop candidate. Do not run multiple competing orchestration kernels in production unless a new measured proof demonstrates a concrete requirement.

Full-product truth: historical Core percentages do not equal completion of the expanded Full Product Vision. A backend subsystem is not finished until its actual packaged Windows user journey is connected and proven. Telegram is removed from active roadmap scope; old Telegram references in historical reuse documents are non-binding unless a future explicit user request reintroduces such a workspace.

Parallel-first rule: every cycle selects the maximum set of genuinely independent large coherent lanes that can advance safely. Dependencies constrain merge/integration order, not isolated research, contract design, adapter implementation, mocks, fixtures, tests or prototypes. A blocked lane must not idle unrelated lanes. Avoid fake parallelism and shared-file collisions; prefer clear lane ownership and stable ports. Use PREPARED / IMPLEMENTED / GREEN / INTEGRATED / PACKAGED / HUMAN_TESTED / NVDA_VERIFIED evidence states accurately.

Large-batch rule: do not stop after one file, one function, one lint error or one small PR if the same coherent subsystem can safely be carried through implementation, error/recovery behavior, tests, docs, CI and integration in the same cycle.

Manual Deep Research development: when the user creates manual Deep Research developer/auditor chats, treat developer chats as real coding lanes capable of large implementation batches, not research-only roles. Their paired auditors independently inspect live GitHub evidence. Scheduled workers should then be paused or reassigned to complementary non-colliding QA/release/integration/evidence work rather than duplicating the same source ownership.

Accessibility: blind primary user, Windows 11 + NVDA. Web-style desktop UI uses local semantic HTML inside pywebview/WebView2; keyboard-only operation, accessible names/roles, headings/landmarks, deterministic focus and text logs are mandatory. Packaged WebView2 UI Automation discovery is a specific gate. Automated accessibility tests do not equal human NVDA verification.

Hotkeys: every application command has a stable Action Registry ID and all application-specific shortcuts are user-remappable through the Keymap system. Do not scatter hard-coded shortcuts through UI code or break standard editing keys.

Safety: no secrets in repo; no token/session/browser profile files; dangerous send/delete/publish/financial/code-execution actions remain governed by preview/audit/approval. Runtime agents never self-modify production source directly or self-expand permissions. A Toolsmith/Software Factory capability proposal uses isolation + tests + compatibility/security gates before registration. For persistent user credentials prefer OS-backed secret storage rather than plaintext configuration. Public-repository secret scanning is a permanent gate.

Git discipline: `main` must remain releasable. Use feature/fix branches and coherent commits. Independent lanes branch from the latest green main unless a real dependency requires otherwise. Never claim success without exact test evidence. Distinguish IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.

CI policy: coherent PR/main gates execute the shared verification harness on both Ubuntu and Windows. Focused Windows/WebView2/package/security/model-hardware jobs may be added where they provide real evidence. Never weaken a check to obtain green. Stale runs for the same PR/ref may be canceled. Do not rebuild an EXE or download large models on every development push.

At the end of every cycle update canonical GitHub status with branch/SHA, changes, tests, blocker, Core-vs-Full-Product truth, parallel lane state changes and next large coherent work wave. User-facing reports should explain practical capabilities first; exact technical evidence remains available underneath.
