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
- M4 model gateway/tools/MCP: IMPLEMENTED candidate / acceptance execution active / not yet INTEGRATED, 0% of its 8% weight credited.
- Overall proven final A–Z product progress remains **36.0%** until an exact M4 candidate satisfies all gates and is merged.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.

## M4 candidate — current cycle
Branch: `dev/m4-model-tools-mcp`.
Implementation head immediately before this status-sync commit: `6e8b649f587ee7202f27028f11fd679e4fbb7171`.
PR: #10, open; merge only after exact green acceptance evidence.

IMPLEMENTED/PREPARED in this coherent M4 slice:
- provider-neutral model request/response/error/capability contracts;
- explicit no-LLM/local/cloud provider kinds and privacy classes;
- provider registry/default routing with fail-closed ambiguity and sensitive-data routing checks;
- deterministic no-LLM mock provider;
- OpenAI-compatible async HTTP adapter and narrow Ollama local adapter using HTTPX;
- deadline/cancellation/error normalization and audit-safe model execution;
- standardized tool specs/calls/results with risk classes, timeout and approval boundary;
- official MCP Python SDK v2 client boundary translating discovered/called tools into Nika contracts;
- direct risky MCP invocation now fails closed without explicit approval, so bypassing `ToolExecutor` cannot silently bypass the risk boundary;
- deterministic tests for mock gateway, controlled HTTP transport, typed provider errors, timeout, cancellation, sensitive-data rejection, tool approval and timeout;
- official MCP SDK in-process server/client proof for discovery, generated input schema and structured tool invocation;
- focused live-provider CI job that installs local Ollama, pulls `smollm2:135m-instruct-q5_K_M`, and calls it through the same `ModelGateway`/`OllamaProvider` contracts;
- canonical M4 design/reuse/acceptance note `docs/M4_MODEL_TOOLS_MCP.md`.

## M4 reuse gate
- REUSE HTTPX for async HTTP transport.
- REUSE official MCP Python SDK v2; do not implement MCP wire protocol.
- ADAPT OpenAI-compatible HTTP for cloud/local compatible providers.
- CUSTOM (thin) direct Ollama configuration, Nika routing/privacy/audit/tool-risk contracts.
- LiteLLM remains optional and is not required by the narrow M4 execution path without measured provider-breadth benefit.

## CI evidence and defects this cycle
- Core CI run 102 reached Ubuntu and Windows runners and exposed one Ruff defect in `src/nika_core/tools.py`; it was fixed rather than bypassed.
- A later executable run exposed an unused import in the expanded M4 proof tests; it was also fixed without weakening Ruff.
- Run 106 proved the expanded shared verification green on Ubuntu before later commits cancelled the superseded Windows job; that superseded partial result is not milestone evidence.
- The final candidate must still produce one exact-head CI result with shared Ubuntu + Windows verification plus the focused live Ollama provider job. No cancelled/superseded run is credited.

## M4 acceptance remaining at this status sync
- Execute the exact final branch head through shared Ubuntu + Windows verification.
- Execute the exact final branch head through the focused live local Ollama proof.
- Confirm the official MCP in-process discovery/call and fail-closed approval regression tests pass in that same exact-head suite.
- Merge PR #10 only after all required jobs are green.
- Until merge, M4 receives 0% milestone credit and overall proven progress remains 36.0%.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. PR #11 is a separate draft downstream M5–M8 foundation lane and is not counted as M4 or integrated product evidence. Parallel-first rules remain present in the canonical board and architecture documents.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4: IMPLEMENTED candidate; acceptance execution active; not INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M5–M12: PREPARED/parallel work only unless separately evidenced; draft PR #11 does not receive milestone credit.
- No Windows standalone package yet.
- No human NVDA verification yet.

## Current milestone
M4 — Model Gateway + standardized tools + MCP integration.

## Next LARGE coherent batch
Finish the exact-head M4 acceptance run. If any Ruff/compile/pytest/MCP/Ollama failure appears, repair it on the same M4 branch and rerun without weakening the gates. If the exact candidate is green across shared Ubuntu/Windows plus live Ollama, merge PR #10, sync canonical status/dashboard on green main, and only then credit M4's 8% weight. Otherwise keep proven progress at 36.0% and report the precise remaining blocker.
