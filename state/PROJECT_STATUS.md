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
- M4 model gateway/tools/MCP: GREEN / INTEGRATED, 100% of its 8% weight.
- Overall proven final A–Z product progress is **44.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI run 112 passed shared Ubuntu verification, shared Windows verification, and the focused live Ollama provider proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.

## M4 integrated evidence
- provider-neutral Nika-owned `ModelRequest`, `ModelResponse`, provider capability and typed error contracts;
- deterministic no-LLM, OpenAI-compatible HTTP and local Ollama providers behind the same Model Gateway interface;
- explicit local/cloud/no-LLM routing with fail-closed ambiguous selection and sensitive-data routing protection;
- timeout, cancellation and provider-error normalization with audit-safe event payloads;
- standardized tool specs/calls/results with risk classes, deadlines and explicit approval boundary;
- official MCP Python SDK v2 adapter for discovery/calls instead of custom protocol implementation;
- official MCP SDK in-process server/client tests for discovery, generated input schema and structured invocation;
- direct risky MCP calls fail closed without explicit approval;
- controlled HTTP transport tests cover provider response and typed 429 failure mapping;
- live acceptance job installed Ollama, pulled `smollm2:135m-instruct-q5_K_M`, and successfully invoked it through Nika `ModelGateway`/`OllamaProvider` on the exact M4 candidate.

## M4 reuse gate
- REUSE HTTPX for async HTTP transport.
- REUSE official MCP Python SDK v2; no custom MCP wire protocol.
- ADAPT OpenAI-compatible HTTP for compatible cloud/local endpoints.
- CUSTOM (thin) direct Ollama configuration plus Nika routing/privacy/audit/tool-risk product semantics.
- LiteLLM remains optional rather than becoming mandatory for the narrow core path without a measured need.

## Defects found and repaired during M4 acceptance
- Core CI run 102 exposed a Ruff import-placement defect in `src/nika_core/tools.py`; fixed without weakening Ruff.
- A later executable run exposed an unused import in expanded M4 proof tests; fixed without weakening checks.
- A manual safety review found that direct `MCPClientAdapter.call()` could otherwise bypass the higher-level approval path; the adapter now fails closed for risky calls without explicit approval and has a regression test.
- Superseded/cancelled runs were never credited. M4 weight was granted only after exact-head run 112 completed all required jobs successfully and PR #10 was merged.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. PR #11 is a separate draft downstream M5–M8 foundation lane and has no product-weight credit unless its own exact acceptance evidence is satisfied and it is integrated. Parallel-first rules remain canonical.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M5–M12: PREPARED/parallel work only unless separately evidenced; draft PR #11 does not receive milestone credit.
- No standalone Windows package yet.
- No human NVDA verification yet; automation must never award NVDA_VERIFIED.

## Current weighted milestone
M5 — accessible Windows UI foundation and host/accessibility proof.

## Next LARGE coherent batch
Re-audit the current M5 upstream stack (pywebview + EdgeChromium/WebView2 and React + TypeScript + Vite + React Aria Components), inspect draft PR #11 rather than assuming it is correct, and build the largest safe M5 slice: local web-style shell, validated backend bridge, Action Registry/Keymap wiring, semantic landmarks/headings/forms/live status/focus restoration, Windows WebView2 host boundary tests and automated accessibility semantics. Keep real NVDA verification human-only. Merge and credit M5 only after its exact acceptance gates are green; otherwise keep proven progress at 44.0%.
