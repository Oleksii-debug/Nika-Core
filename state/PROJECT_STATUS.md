# PROJECT STATUS — Nika Core

Updated: 2026-08-17
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- Overall final A–Z product: 6% after M0 bootstrap/CI is green.
- M0 Research/adoption/governance/bootstrap: target 100% for this bootstrap.
- M1–M12: 0% until their acceptance gates begin closing.

## Current milestone
M0 — repository/bootstrap/governance.

## Proven prepared bootstrap
- deterministic Python kernel bootstrap;
- SQLite schema initialization;
- task state machine and event history;
- task queue;
- agent registry;
- checksum-backed checkpoint service;
- provider-neutral model protocol + mock provider;
- six local unit tests pass in the prepared bootstrap;
- cheap Linux GitHub CI definition.

## Architecture decisions
LangGraph primary orchestration runtime; Deep Agents selective harness; LiteLLM behind ModelGateway; MCP Python SDK v2 for tool interoperability; APScheduler stable 3.x behind SchedulerPort; PySide6 Widgets GUI baseline; pyside6-deploy/Nuitka packaging; AutoGen/CrewAI evaluated alternatives rather than simultaneous core runtimes.

## Next coherent batch
M1.1: integrate versioned configuration/schema contracts and LangGraph runtime/checkpointer adapter while preserving deterministic task/audit ownership; add restart/resume proof tests.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation. Oleksii performs human NVDA acceptance for relevant candidates.
