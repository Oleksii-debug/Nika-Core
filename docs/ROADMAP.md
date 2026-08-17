# Nika Core — A–Z roadmap and weighted progress model

Baseline date: 2026-08-17. Status values: NOT_STARTED / ACTIVE / BLOCKED / GREEN / RELEASED.

Progress is acceptance-gate weighted, not commit-count based. Code that is merely written but not integrated/tested receives partial or zero credit. Regressions may reduce progress.

| Stage | Weight | Goal |
|---|---:|---|
| M0 Research, adoption, governance & bootstrap | 6% | final architecture, dependency adoption map, repo rules, canonical status, cheap CI |
| M1 Kernel foundation | 10% | config, registries, SQLite schema/migrations, task state, audit/events, workspace contract |
| M2 Durable agent runtime | 11% | LangGraph run loop, cancellation, retries, approvals, bounded planning, durable checkpoint/resume |
| M3 Memory, scheduler & resource control | 9% | memory namespaces, APScheduler adapter, resource budgets, queue fairness |
| M4 Model Gateway, tools & MCP | 8% | mock/no-LLM, Ollama, cloud/OpenAI-compatible, LiteLLM adapter, MCP tool layer |
| M5 Accessible Windows GUI | 11% | PySide6 Widgets, keyboard-first UI, NVDA semantics, logs/tasks/agents/workspaces |
| M6 Agent Builder & permissions | 8% | NL draft -> schema -> permission review -> versioned activation |
| M7 Multi-agent laboratory | 9% | supervisor/subagents, teams, typed handoffs, parallel fan-out, quotas, evaluator |
| M8 Self-learning & experiment engine | 10% | metrics, replay, prompt/strategy versions, champion/challenger, rollback, optional DSPy |
| M9 Plugin SDK & real workspaces | 8% | stable plugin/workspace API and at least two independent real workspaces |
| M10 Security, sandbox & reliability | 5% | sandbox, secrets, backup/restore, corruption/crash recovery, threat hardening |
| M11 Windows packaging & distribution | 3% | standalone EXE/ZIP, manifest/checksums/licenses and no-Python execution |
| M12 Full-system QA, NVDA acceptance & v1.0 | 2% | full P0 gates, recovery drill, human NVDA acceptance, production release |

Total: 100%.

## Current baseline
After the bootstrap commit and its CI are green: M0 = 100%, overall final product = 6%. M1–M12 begin at 0 until gates close.

## Normal critical path
M0 -> M1 -> M2 -> M3/M4 -> M5 -> M6 -> M7 -> M8 -> M9 -> M10 -> M11 -> M12. Independent work may overlap only after contracts stabilize.

## Actions/minutes policy
Every PR gets cheap Linux compile/lint/unit tests. Windows hosted runner is reserved for milestone integration, Windows-specific defects, accessibility/package gates and release candidates. Do not build EXE every hourly run. Python-source tests are the normal development loop; package when a milestone needs user/release verification.
