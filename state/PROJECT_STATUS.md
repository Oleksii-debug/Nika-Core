# PROJECT STATUS — Nika Core

Updated: 2026-08-17
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Weighted progress
- Before bootstrap CI proof: M0 = 90% of its 6% weight; overall final A–Z product = 5.4%.
- After exact bootstrap CI is green: M0 = 100%; overall = 6.0%.
- M1–M12 begin at 0 until acceptance-gate evidence closes work.

## Current milestone
M0 — research/reuse audit/repository/bootstrap/governance.

## Prepared bootstrap
- deterministic Python kernel bootstrap;
- SQLite schema initialization;
- task state machine and event history;
- task queue;
- agent registry;
- checksum-backed checkpoint service;
- provider-neutral model protocol + mock provider;
- six local unit tests previously pass in the prepared bootstrap;
- cheap Linux GitHub CI definition;
- reuse-before-rewrite policy and third-party adoption map;
- UI direction corrected to local web-style HTML/CSS/JS in pywebview/WebView2, based on Accessible Chess lessons;
- mandatory configurable Action Registry/Keymap requirement.

## Adopted upstream blocks
LangGraph; langgraph-checkpoint-sqlite; selective Deep Agents; LiteLLM; MCP Python SDK v2; APScheduler stable 3.x; pywebview/WebView2; PyInstaller for Windows freezing; optional DSPy later. AutoGen/CrewAI retained as evaluated alternatives/adapters, not parallel kernel runtimes.

## Next coherent batch
Close M0 with exact GitHub CI on the bootstrap, then M1.1: versioned configuration/schema contracts + persisted registries + Action Registry/keymap schema and tests. Next M2 begins LangGraph durable adapter and restart/resume proof.

## Packaging policy
Do not spend build time/minutes generating EXE for every change. Develop/test normally from Python. Build Windows standalone at milestone/user-test/release gates; final product must run without Python.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation. Oleksii performs human NVDA acceptance for relevant candidates.
