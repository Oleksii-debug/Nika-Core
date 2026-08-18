# Nika Core — A–Z roadmap and weighted progress model

Baseline created: 2026-08-17. Current status synchronized: 2026-08-18.
Progress is acceptance-gate weighted, not commit-count based. Regressions may reduce progress. Source work may proceed in parallel; product credit is awarded only after the relevant exact acceptance gate is green and integrated.

| Stage | Weight | Goal | Current truth |
|---|---:|---|---|
| M0 Research, reuse audit, governance & bootstrap | 6% | final architecture, adoption map, repo rules, status, CI | GREEN / INTEGRATED |
| M1 Kernel foundation | 10% | typed config, persisted registries, SQLite migrations, task state, audit/events, workspace/plugin contract, Action Registry/keymap | GREEN / INTEGRATED |
| M2 Durable agent runtime | 11% | AgentRuntimePort, runtime selection, run loop, cancellation, retries, approvals, durable checkpoint/resume | GREEN / INTEGRATED |
| M3 Memory, scheduler & resource control | 9% | memory namespaces, SchedulerPort/APScheduler adapter, resource budgets, queue fairness | GREEN / INTEGRATED |
| M4 Model Gateway, tools & MCP | 8% | mock/no-LLM, Ollama, cloud/OpenAI-compatible, provider adapter, MCP tool layer | GREEN / INTEGRATED |
| M5 Accessible web-style Windows GUI | 11% | local frontend + pywebview/WebView2 shell, keyboard/NVDA semantics, shortcut editor, logs/tasks/agents/workspaces | GREEN / INTEGRATED |
| M6 Agent Builder & permissions | 8% | natural-language draft -> schema -> permission review -> versioned activation | GREEN / INTEGRATED |
| M7 Multi-agent laboratory | 9% | supervisor/subagents, teams, typed handoffs, parallel fan-out, quotas, evaluator | GREEN / INTEGRATED |
| M8 Self-learning & experiment engine | 10% | metrics, replay, prompt/strategy versions, champion/challenger, rollback, optional DSPy | GREEN / INTEGRATED |
| M9 Plugin SDK & real workspaces | 8% | stable plugin/workspace API and real independent workspaces | GREEN / INTEGRATED |
| M10 Security, sandbox & reliability | 5% | sandbox, secrets, backup/restore, corruption/crash recovery, threat hardening | GREEN / INTEGRATED |
| M11 Windows packaging & distribution | 3% | standalone EXE/ZIP, local assets, manifest/checksums/licenses, no-Python execution | GREEN / INTEGRATED / PACKAGED candidate evidence |
| M12 Full-system QA, NVDA acceptance & v1.0 | 2% | full P0 gates, recovery drill, human NVDA acceptance, production release | AUTOMATED PRE-HUMAN GATE GREEN / INTEGRATED; HUMAN GATE OPEN |

Total: 100%.

## Current proven progress
M0–M11 and the automated pre-human portion of M12 are GREEN / INTEGRATED. The final human-only M12 credit remains intentionally unawarded. Overall proven final A–Z product progress is therefore **98.0%**.

- HUMAN_TESTED: false.
- NVDA_VERIFIED: false.
- PRODUCTION_RELEASE_READY: false.
- The final 2% may be awarded only after the exact packaged candidate passes the human Windows/NVDA protocol in `docs/M12_HUMAN_NVDA_ACCEPTANCE.md`.

Canonical detailed truth is `state/PROJECT_STATUS.md`; parallel lane ownership/evidence states are in `state/PARALLEL_EXECUTION_BOARD.md`.

## Reuse-first implementation map
The current A–Z component audit is `docs/REUSE_CATALOG_2026-08-18.md`. Every lane uses **REUSE -> ADAPT -> CUSTOM (thin)** by default. Do not build generic schedulers, browser engines, Windows automation stacks, coding agents, model-provider gateways, OCR/speech engines, vector databases, retry engines, resource monitors or packaging systems from scratch when a maintained compatible component satisfies the requirement.

## Integrated milestone summary

### M1 — Kernel foundation
Typed settings; ordered backward migrations; persisted Agent/Workspace registries; generic audit log; stable workspace plugin discovery contract; central Action Registry and persisted remappable Keymap with conflict/clear/restore/import/export behavior are integrated.

### M2 — Durable runtime
LangGraph is integrated behind framework-neutral `AgentRuntimePort`; local durable resume/crash recovery, explicit approval, cancellation, bounded retry, side-effect idempotency/reconciliation and startup recovery are proven. Microsoft Agent Framework remains a secondary migration/interop candidate rather than a simultaneous production kernel.

### M3 — Memory, scheduler and resources
Durable scoped memory, explicit user-memory consent, expiration/purge, APScheduler-backed persistent schedules, restart/pause/resume semantics, resource budgets, psutil observation and FIFO resource fairness are integrated.

### M4 — Model Gateway, tools and MCP
Provider-neutral Model Gateway, no-LLM/OpenAI-compatible/Ollama adapters, privacy-aware routing, typed provider failures, guarded standardized tools, official MCP SDK v2 adapter and a live Ollama same-interface proof are integrated.

### M5 — Accessible Windows UI
Native semantic local web UI hosted by pywebview + EdgeChromium/WebView2, narrow validated backend bridge, centralized configurable Action Registry/Keymap, live textual status and deterministic focus are integrated. Packaged WebView2 UI Automation descendant discovery and keyboard/focus proofs passed. Automated accessibility evidence does not equal human NVDA verification.

### M6 — Agent Builder and permissions
Versioned strict Pydantic agent definitions, Model Gateway natural-language drafting with schema validation, deterministic registry-backed compilation, fail-closed R0–R4 permission review, immutable definition persistence, persisted high-impact approval requirements and atomic activation are integrated.

### M7 — Multi-agent laboratory
Durable team/member lineage, typed handoff/result contracts, bounded fan-out through the existing runtime port, persisted depth/size/concurrency quotas, fail-closed privilege attenuation, cancellation propagation, restart-safe member identity and deterministic evaluator aggregation are integrated.

### M8 — Experiment engine
Immutable experiment definitions bind strategy/config candidates to fixed replay/dataset references, metrics and unchanged permissions. Deterministic champion/challenger evaluation, promotion denial, promotion, rollback, append-only evidence, stale-writer protection and crash-safe lifecycle transitions are integrated.

### M9 — Plugin SDK and real workspaces
Versioned plugin/workspace compatibility, capability and risk contracts are integrated. Software Factory runs behind `CodingWorkerPort`; Accessibility Repair/Assistant follows semantic-first interaction with visual/coordinate fallback only after semantic inspection. Optional Playwright ARIA and Windows UIA adapters have live CI proofs.

### M10 — Security, sandbox and reliability
Traversal-safe workspace boundaries, exact network/process allowlists, bounded execution budgets, expiring single-use approval evidence and fail-closed authorization order are integrated. The project explicitly claims defense-in-depth policy rather than pretending to provide a complete OS-level sandbox.

### M11 — Windows packaging
PyInstaller one-dir/windowed packaging, local WebView assets, deterministic release manifest/checksums and packaged WebView2/UIA/keyboard/focus verification are integrated. The final user candidate runs without requiring Python. M11 packaging evidence is distinct from final human release acceptance.

### M12 — Automated pre-human release gate
Exact candidate `d7bdfd697819adf13ad7423726a004fd781d857d` passed Core CI 200 and M12 Pre-Human Release Gate run 1. Ubuntu and Windows passed complete verification plus focused recovery/safety matrices; Windows also built and verified the packaged candidate, release manifest, WebView2/UIA semantics and Action Registry keyboard/focus flow. Artifact identity and digest are recorded in `state/PROJECT_STATUS.md` and Issue #1. Machine-readable evidence deliberately records `human_tested=false`, `nvda_verified=false` and `production_release_ready=false`.

## Parallel-first execution model
Nika uses dependency-aware parallel development. Dependencies constrain integration order, not independent research, contract work, fixtures, mocks or isolated adapter implementation. The binding policy is `docs/PARALLEL_DEVELOPMENT_POLICY.md`.

At the current 98% pre-human release state, the M12 release-freeze exception applies: production feature expansion pauses because changing behavior would invalidate the exact packaged candidate already bound to human acceptance. Safe autonomous work is limited to evidence/status consistency, protocol clarification and concrete defect investigation until human Windows/NVDA results justify a new candidate.

## CI policy
Coherent PR/main gates use the same verification harness on Ubuntu and Windows where applicable. Independent OS jobs run with fail-fast disabled so one platform does not hide evidence from the other. Windows/WebView2/package/accessibility-specific proofs add focused jobs/artifacts only where they provide real evidence. Do not build a release EXE on every source push.

## Packaging policy
The base Windows product remains small and model-independent. Large browser/coding/speech/OCR/vision/model workers and their model files are optional components. Application updates must not require redownloading local AI models or user data.

## Final blocker
The only weighted blocker is human execution of `docs/M12_HUMAN_NVDA_ACCEPTANCE.md` against the exact M12 packaged candidate. If human testing finds a defect, create a new development candidate, fix the defect, rerun the full M12 automated gate, and repeat human acceptance on that same exact artifact. Only a human PASS may award HUMAN_TESTED, NVDA_VERIFIED and the final 2%.
