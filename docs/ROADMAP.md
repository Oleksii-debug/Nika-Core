# Nika Core — A–Z roadmap and weighted progress model

Baseline date: 2026-08-17. Status values: NOT_STARTED / ACTIVE / BLOCKED / GREEN / RELEASED.
Progress is acceptance-gate weighted, not commit-count based. Regressions may reduce progress.

| Stage | Weight | Goal |
|---|---:|---|
| M0 Research, reuse audit, governance & bootstrap | 6% | final architecture, adoption map, repo rules, status, cheap CI |
| M1 Kernel foundation | 10% | typed config, persisted registries, SQLite migrations, task state, audit/events, workspace/plugin contract, Action Registry/keymap |
| M2 Durable agent runtime | 11% | AgentRuntimePort, framework proof/selection, run loop, cancellation, retries, approvals, bounded planning, durable checkpoint/resume |
| M3 Memory, scheduler & resource control | 9% | memory namespaces, SchedulerPort/APScheduler adapter, resource budgets, queue fairness |
| M4 Model Gateway, tools & MCP | 8% | mock/no-LLM, Ollama, cloud/OpenAI-compatible, provider adapter, MCP tool layer |
| M5 Accessible web-style Windows GUI | 11% | local HTML/CSS/JS + pywebview/WebView2 shell, keyboard/NVDA semantics, configurable shortcut editor, logs/tasks/agents/workspaces |
| M6 Agent Builder & permissions | 8% | natural-language draft -> schema -> permission review -> versioned activation |
| M7 Multi-agent laboratory | 9% | supervisor/subagents, teams, typed handoffs, parallel fan-out, quotas, evaluator |
| M8 Self-learning & experiment engine | 10% | metrics, replay, prompt/strategy versions, champion/challenger, rollback, optional DSPy |
| M9 Plugin SDK & real workspaces | 8% | stable plugin/workspace API and at least two independent real workspaces |
| M10 Security, sandbox & reliability | 5% | sandbox, secrets, backup/restore, corruption/crash recovery, threat hardening |
| M11 Windows packaging & distribution | 3% | standalone EXE/ZIP, local assets, manifest/checksums/licenses, no-Python execution |
| M12 Full-system QA, NVDA acceptance & v1.0 | 2% | full P0 gates, recovery drill, human NVDA acceptance, production release |

Total: 100%.

## Current baseline
M0 is GREEN at 100%; overall proven product progress = 6.0% before M1 integration evidence. M1 closes only after its coherent foundation branch passes PR/main CI and the persisted config/registry/migration/keymap tests are green.

## M1 acceptance slice
M1 is one coherent foundation milestone, not micro-tickets: typed settings; ordered backward migrations; persisted Agent/Workspace registries; generic audit log; stable workspace plugin discovery contract; central Action Registry and persisted remappable Keymap with conflict/clear/restore/import/export behavior.

## M2 selection gate
Before choosing the primary orchestration runtime, compare current LangGraph and Microsoft Agent Framework behind `AgentRuntimePort` with the same Nika proof scenario. Do not let either framework's public types become Nika domain contracts.

## Normal critical path
M0 -> M1 -> M2 -> M3/M4 -> M5 -> M6 -> M7 -> M8 -> M9 -> M10 -> M11 -> M12. Independent work may overlap only after contracts stabilize.

## Actions/minutes policy
Development branches use source/static/unit checks without triggering hosted Actions on every push. Coherent PR/main gates use cheap Linux CI. Windows hosted runner is reserved for Windows/WebView2-specific defects, accessibility/package gates and release candidates. Do not build EXE every development cycle.
