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
- M5 accessible web-style Windows GUI: GREEN / INTEGRATED, 100% of its 11% weight.
- M6 Agent Builder & permissions: GREEN / INTEGRATED, 100% of its 8% weight.
- Overall proven final A–Z product progress is **63.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI run 112 passed Ubuntu, Windows, and focused live Ollama provider proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 exact green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI run 137 passed shared Ubuntu verification, shared Windows verification, and the packaged PyInstaller/WebView2 UI Automation + keyboard/focus proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
- M6 exact green head `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI run 142 passed shared Ubuntu and Windows verification; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.

## M6 integrated evidence
- versioned Pydantic `AgentDefinition` format with strict extra-field rejection, bounded fields, duplicate-tool rejection and JSON Schema export;
- natural-language drafting goes through the integrated provider-neutral Model Gateway, but returned JSON is only a proposal and must pass strict Pydantic validation;
- deterministic compiler resolves every model profile, schedule, resource budget and tool against current Nika registries and fails closed for unknown references;
- current standardized `ToolRisk` values are mapped to explicit Nika R0/R1/R2/R4 tiers; risk declarations must match the registered tool classification and cannot be lowered or exaggerated by model output;
- R4/high-impact capability requirements are compiled and persisted with the immutable draft instead of trusting the caller at activation time;
- SQLite migration v5 stores immutable agent-definition versions, compiled approval requirements and highest risk, with a unique one-active-version-per-agent invariant;
- activation re-reads the persisted immutable draft and persisted R4 approval requirements, rejects mutation/missing approval, atomically retires the previous active version, activates the reviewed version and appends audit evidence;
- Agent Builder activation approval does not replace execution-time approval in the M4 tool boundary: high-impact invocations remain separately guarded;
- acceptance regression tests cover unknown tools/model references, risk mismatch, approval bypass, immutable mutation, version retirement/activation, schema v5 migration and invalid Model Gateway draft output.

## M6 reuse gate
- REUSE Pydantic v2 validation and JSON Schema rather than adding a second schema framework.
- ADAPT the integrated Nika Model Gateway for structured drafting only; the model never owns permission truth or activation.
- REUSE the existing standardized `ToolSpec` / `ToolRisk`, SQLite migration layer and `AuditLog`.
- CUSTOM (thin): Nika-specific R0–R4 compilation, immutable activation/versioning and human-review policy.
- No new third-party dependency was added for M6.

## Defects found and repaired during M6 acceptance
- Manual safety review found that an early repository shape could have trusted caller-supplied R4 approval requirements. Before acceptance, requirements were moved into the persisted compiled draft so activation derives them from authoritative stored state.
- Core CI run 141 then found two Ruff `ISC004` errors in migration SQL string formatting on both Ubuntu and Windows. The SQL was reformatted without weakening Ruff or changing the migration semantics.
- Exact-head Core CI run 142 subsequently passed the complete shared verification on both Ubuntu and Windows. Superseded/failed candidates were not credited.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. `AGENTS.md` currently references `docs/PARALLEL_DEVELOPMENT_POLICY.md`, but that file is absent from main and exists only in the stale PR #4 proposal; this governance inconsistency remains unintegrated and must not be treated as accepted policy text beyond the parallel-first rules already present in integrated canonical documents. PR #11 remains a separate draft downstream M5–M12 foundation lane and receives no product-weight credit unless each reusable slice is independently reviewed, proven and integrated.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M5: IMPLEMENTED / GREEN / INTEGRATED; diagnostic packaged acceptance proof exists, but this is not an M11 release package; not HUMAN_TESTED; not NVDA_VERIFIED.
- M6: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED; no NVDA claim.
- M7–M12: PREPARED/parallel work only unless separately evidenced; draft PR #11 does not receive milestone credit.
- No standalone release Windows package yet.
- No human NVDA verification yet; automation must never award NVDA_VERIFIED.

## Current weighted milestone
M7 — Multi-agent laboratory.

## Next LARGE coherent batch
Build the largest safe M7 multi-agent slice on a fresh branch from the latest green `main`: durable supervisor/child identity behind `AgentRuntimePort`, typed handoff/result contracts, bounded fan-out, spawn-depth and concurrency quotas, privilege attenuation so children can never gain permissions absent from the parent, cancellation propagation, restart-safe parent/child evidence and evaluator aggregation. Reuse LangGraph graph/subgraph and durable execution primitives behind Nika-owned contracts; do not add a second production orchestration kernel. Add deterministic fault/restart/approval/quota tests and integrate only after exact Ubuntu + Windows acceptance evidence is green. Keep M8–M12 parallel research/preparation isolated and uncredited until their own gates.
