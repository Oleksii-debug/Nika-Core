# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT / PARALLEL-FIRST
Repository visibility observed this cycle: PUBLIC.

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- M2 durable agent runtime: GREEN / INTEGRATED, 100% of its 11% weight.
- M3 memory/scheduler/resource control: GREEN / INTEGRATED, 100% of its 9% weight.
- M4 model gateway/tools/MCP: IMPLEMENTED candidate / not yet GREEN or INTEGRATED, 0% of its 8% weight credited.
- Overall proven final A–Z product progress remains **36.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.

## M4 candidate — this cycle
Branch: `dev/m4-model-tools-mcp`.
Candidate head before this status commit: `2b0cd22cf9c4c23d1d667349061c01c3738091dd`.

IMPLEMENTED/PREPARED in this batch:
- provider-neutral model request/response/error/capability contracts;
- explicit no-LLM/local/cloud provider kinds and privacy classes;
- provider registry/default routing with fail-closed ambiguity and sensitive-data routing checks;
- deterministic no-LLM mock provider;
- OpenAI-compatible async HTTP adapter and narrow Ollama local adapter using HTTPX;
- deadline/cancellation/error normalization and audit-safe model execution;
- standardized tool specs/calls/results with risk classes, timeout and approval boundary;
- official MCP Python SDK v2 client boundary translating discovered/called tools into Nika contracts;
- deterministic tests for mock gateway routing, sensitive-data fail-closed behavior, approval and timeout;
- canonical M4 design/reuse note `docs/M4_MODEL_TOOLS_MCP.md`.

## M4 reuse gate
- REUSE HTTPX for async HTTP transport.
- REUSE official MCP Python SDK v2; do not implement MCP wire protocol.
- ADAPT OpenAI-compatible HTTP for cloud/local compatible providers.
- CUSTOM (thin) direct Ollama configuration, Nika routing/privacy/audit/tool-risk contracts.
- LiteLLM remains optional and is not pulled into the mandatory path without measured provider breadth benefit.

## M4 acceptance still unverified
- No exact-head Ubuntu/Windows CI result yet for this candidate.
- Real-provider same-interface proof is still required by `docs/ACCEPTANCE_GATES.md`; intended first proof is controlled local Ollama/OpenAI-compatible HTTP, not a secret-bearing cloud call in CI.
- MCP discovery/call proof against an in-process/controlled official SDK server still needs executable CI evidence.
- No M4 weight may be credited before exact green evidence and integration.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. Parallel-first rules remain present in the current canonical architecture/roadmap/board documents.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4: IMPLEMENTED candidate; not GREEN; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M5–M12: PREPARED/parallel lanes only unless separately evidenced.
- No Windows standalone package yet.
- No human NVDA verification yet.

## Current milestone
M4 — Model Gateway + standardized tools + MCP integration.

## Next LARGE coherent batch
Open/execute the M4 PR gate. Fix every real Ruff/compile/pytest issue on the same branch. Add executable MCP in-process proof and controlled real-provider proof through the same Nika interface. Merge only after the exact candidate passes Ubuntu + Windows and all Model Gateway acceptance requirements; otherwise keep overall proven progress at 36.0%.
