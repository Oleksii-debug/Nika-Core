# Nika Core autonomous development instructions

This repository is the canonical source of truth. Chat history is not.

Before every coding cycle read: docs/MASTER_SPEC.md, docs/ROADMAP.md, docs/THIRD_PARTY_ADOPTION.md, docs/LARGE_BATCH_POLICY.md, docs/PARALLEL_DEVELOPMENT_POLICY.md, docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md, docs/ACCEPTANCE_GATES.md, state/PROJECT_STATUS.md, LIVE DASHBOARD, open PRs and current CI.

Primary rule: REUSE BEFORE REWRITE. Search maintained upstream libraries and current official documentation before implementing a subsystem. Record REUSE, ADAPT or CUSTOM. Do not copy random third-party source into this repository when a package dependency/adapter is sufficient.

Development mode: PARALLEL-FIRST and DEPENDENCY-AWARE. Roadmap milestones are maturity/acceptance gates, not a command to implement the product serially. At the start of every cycle inspect all roadmap areas, build/update the dependency graph, and advance the maximum set of genuinely independent large coherent packages. Normally target 5-10 active workstreams when enough safe independent work exists. Prefer fewer deep packages over many trivial edits. A blocker in one workstream must never idle unrelated workstreams. Preparation and implementation may proceed in parallel behind stable ports/contracts/mocks; integration and merges remain dependency-ordered and acceptance-gated. Follow docs/PARALLEL_DEVELOPMENT_POLICY.md.

Large-batch rule: maximize safe coherent engineering output per cycle. Do not stop at one file/function/test when research, adoption decision, contracts, implementation, persistence, tests, docs and status can form one safe package. Never manufacture shallow work merely to report more lanes.

Architecture: Windows-first modular monolith with ports-and-adapters, dependency inversion, versioned Nika-owned contracts, migrations/backward-compatible evolution and workspace/plugin boundaries. External framework/provider/UI details must not leak into Nika domain contracts. Nika owns task/audit/permission/product contracts; ModelGateway remains provider-neutral; deterministic code handles state/validation/dedup/safety; LLMs are used where semantic reasoning is useful.

Agent runtime selection: do not assume a primary orchestration framework before the M2 proof. Compare the current supported Python lines of LangGraph and Microsoft Agent Framework against Nika's durable-execution criteria. Nika domain depends on AgentRuntimePort rather than framework concrete classes. Choose one primary runtime from evidence; do not attach multiple orchestration frameworks to the kernel without demonstrated need.

Parallel standing workstreams: (1) kernel foundation; (2) durable agent runtime; (3) memory/scheduler/resources; (4) Model Gateway/tools/MCP; (5) accessible Windows web-style UI; (6) Agent Builder/permissions; (7) multi-agent lab; (8) experiment/self-learning engine; (9) plugin/workspace SDK and real workspaces; (10) security/sandbox/packaging/QA foundations. These may advance concurrently when their work is independent and isolated.

Accessibility: blind primary user, Windows 11 + NVDA. Standard accessible semantics, accessible names/roles, logical tab order, keyboard-only operation, text logs/status, no mouse-only flow. The Windows GUI direction is local HTML/CSS/JS hosted through pywebview + EdgeChromium/WebView2, with the current frontend stack audited again before M5 implementation. Packaged WebView2 host accessibility must be tested. Automated accessibility tests never equal human NVDA verification.

Hotkeys: all application shortcuts go through a centralized Action Registry + configurable Keymap. Users must be able to change, clear, restore defaults, search, detect conflicts and export/import mappings. Do not scatter critical hard-coded shortcuts through UI modules. Preserve standard text-editing shortcuts.

Safety: no secrets in repo; no token/session/browser profile files; dangerous send/delete/publish/financial/code-execution actions require preview/audit/approval as specified. Runtime agents never self-modify production source directly. Controlled self-improvement must use proposal -> isolated branch/worktree/sandbox -> tests -> security -> exact-SHA CI -> integration -> QA -> release.

Git discipline: main must remain releasable. Use isolated feature branches/worktrees or isolated modules for parallel work and coherent commits. Avoid parallel edits to the same central file. Never claim success without exact evidence. Distinguish PREPARED, IMPLEMENTED, INTEGRATED, PACKAGED and HUMAN_TESTED; NVDA VERIFIED is human-only.

CI cost: cheap source/unit/static checks frequently; use hosted CI on coherent PR/main/milestone/Windows/WebView2/packaging/release gates. A GitHub billing/runner/Actions blocker in one lane must not stop unrelated local/source-tested work in other lanes. Never weaken gates to make them green. Do not rebuild EXE every hourly cycle.

Packaging: develop source-first. Final Windows release must run without Python. Keep program, optional modules, heavy models and user data separate; never bundle Ollama/Whisper model weights into the base executable.

At the end of every cycle update canonical GitHub status and LIVE DASHBOARD with a parallel-lane matrix: lane/capability, branch/exact SHA, PREPARED/IMPLEMENTED/INTEGRATED/PACKAGED/HUMAN_TESTED state, tests/evidence, blockers, and next large coherent package. Weighted overall progress changes only when acceptance-gate evidence justifies it.
