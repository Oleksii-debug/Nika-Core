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
| M7 Multi-agent laboratory | 9% | supervisor/subagents, teams, typed handoffs, parallel fan-out, quotas, evaluator | NEXT WEIGHTED MILESTONE / NO CREDIT YET |
| M8 Self-learning & experiment engine | 10% | metrics, replay, prompt/strategy versions, champion/challenger, rollback, optional DSPy | PARALLEL LANE / NO CREDIT YET |
| M9 Plugin SDK & real workspaces | 8% | stable plugin/workspace API and real independent workspaces | PARALLEL LANE / NO CREDIT YET |
| M10 Security, sandbox & reliability | 5% | sandbox, secrets, backup/restore, corruption/crash recovery, threat hardening | PARALLEL LANE / NO CREDIT YET |
| M11 Windows packaging & distribution | 3% | standalone EXE/ZIP, local assets, manifest/checksums/licenses, no-Python execution | PARALLEL LANE / NO CREDIT YET |
| M12 Full-system QA, NVDA acceptance & v1.0 | 2% | full P0 gates, recovery drill, human NVDA acceptance, production release | PARALLEL LANE / NO CREDIT YET |

Total: 100%.

## Current proven progress
M0 + M1 + M2 + M3 + M4 + M5 + M6 are GREEN / INTEGRATED. Overall proven final A–Z product progress is therefore **63.0%**. M7–M12 can contain prepared or implemented work, but they receive no weighted credit until their own acceptance evidence is green and integrated.

Canonical detailed truth is `state/PROJECT_STATUS.md`; parallel lane ownership/evidence states are in `state/PARALLEL_EXECUTION_BOARD.md`.

## Reuse-first implementation map
The current A–Z component audit is `docs/REUSE_CATALOG_2026-08-18.md`. Every lane must use **REUSE -> ADAPT -> CUSTOM (thin)** as the default decision order. Do not build generic schedulers, browser engines, Windows automation stacks, coding agents, model-provider gateways, OCR/speech engines, vector databases, retry engines, resource monitors or packaging systems from scratch when a maintained compatible component satisfies the requirement.

## M1 integrated slice
Typed settings; ordered backward migrations; persisted Agent/Workspace registries; generic audit log; stable workspace plugin discovery contract; central Action Registry and persisted remappable Keymap with conflict/clear/restore/import/export behavior are integrated.

## M2 integrated slice
LangGraph is the implemented primary runtime behind framework-neutral `AgentRuntimePort`; async SQLite durability, Nika task-to-runtime recovery mapping, explicit approval, cancellation, bounded retry, idempotency/reconciliation and crash-consistency proofs are integrated. Microsoft Agent Framework remains a secondary migration/interop candidate rather than a simultaneous production kernel.

## M3 integrated slice
Durable scoped memory, explicit user-memory consent, expiration/purge, APScheduler-backed persistent schedules, restart/pause/resume semantics, resource budgets, psutil observation and FIFO resource fairness are integrated.

## M4 integrated slice
Provider-neutral Model Gateway, no-LLM/OpenAI-compatible/Ollama adapters, privacy-aware routing, typed provider failures, guarded standardized tools, official MCP SDK v2 adapter and a live Ollama same-interface proof are integrated.

## M5 integrated slice
Native semantic local web UI hosted by pywebview + explicit EdgeChromium/WebView2, narrow validated backend bridge, centralized configurable Action Registry/Keymap, live textual status and deterministic focus are integrated. Exact-head Core CI run 137 passed Ubuntu, Windows and a packaged PyInstaller one-dir WebView2 UI Automation descendant + keyboard/focus proof. This diagnostic package is M5 evidence, not M11 release packaging. HUMAN_TESTED and NVDA_VERIFIED remain false.

## M6 integrated slice
Versioned strict Pydantic agent definitions, Model Gateway natural-language drafting with schema validation, deterministic registry-backed compilation, fail-closed R0–R4 permission review, immutable SQLite v5 definition persistence, persisted high-impact approval requirements and atomic version activation are integrated. Exact-head Core CI run 142 passed Ubuntu and Windows after CI run 141 exposed and the branch repaired migration-lint defects. Agent configuration approval never bypasses the existing execution-time high-impact tool approval boundary.

## Parallel-first execution model
There is no source-development critical path that says later independent capabilities cannot be prepared while the current weighted milestone is being accepted. M7–M12 may advance concurrently when ownership and contracts allow it.

Rules:
1. Branch independent lanes from the latest green `main` unless they genuinely depend on another unmerged lane.
2. Prefer lane-owned modules/adapters and stable contracts to shared-file edits.
3. Upstream dependency constraints govern merge order, not preparation/implementation against ports/mocks/fixtures.
4. A blocked lane does not block unrelated lanes.
5. Merge/integration remains dependency-aware and requires exact green evidence.
6. HUMAN_TESTED and NVDA_VERIFIED remain separate human gates.

## CI policy
Coherent PR/main gates use the same verification harness on Ubuntu and Windows. Independent OS jobs run with fail-fast disabled so one platform does not hide evidence from the other. Stale runs for the same PR/ref may be canceled to save capacity. Windows/WebView2/package/accessibility-specific proofs may add focused jobs/artifacts. Do not build a release EXE on every source push.

## Packaging policy
The base Windows product remains small and model-independent. Large browser/coding/speech/OCR/vision/model workers and their model files are optional components. Application updates must not require redownloading local AI models or user data.
