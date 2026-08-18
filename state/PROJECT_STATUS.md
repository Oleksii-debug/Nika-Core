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
- M7 multi-agent laboratory: GREEN / INTEGRATED, 100% of its 9% weight.
- Overall proven final A–Z product progress is **72.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI run 112 passed Ubuntu, Windows and focused live Ollama proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 exact green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI run 137 passed Ubuntu, Windows and packaged WebView2/UIA keyboard-focus proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
- M6 exact green head `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI run 142 passed Ubuntu and Windows; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
- M7 exact green head `4feb976faa97949bacc321bcbce792d01359a58c`; Core CI run 150 passed Ubuntu and Windows; PR #17 merged as `5a01692c1372375f040cd38558e33204b082d5a5`.

## M7 integrated evidence
- SQLite migration v6 persists multi-agent teams, members, typed handoffs and member results while retaining ordered backward migration behavior.
- Stable Nika-owned team/member identity stores parent lineage, depth, immutable agent ID/version reference, runtime thread ID, attenuated tool grants, lifecycle state and resume token.
- Typed task/result/status/error handoffs carry explicit team, sender, recipient, handoff and correlation identity.
- Parent-to-child privilege attenuation is fail-closed: children cannot request an unknown parent tool, higher risk tier or broader scopes.
- Persisted `TeamQuota` bounds delegation depth, children per parent, total team size and concurrent execution.
- `MultiAgentSupervisor` reuses the integrated `AgentRuntimePort`; no second production orchestration kernel was introduced.
- Bounded fan-out uses the persisted concurrency budget; one modeled runtime worker failure is recorded against that child while sibling work can complete.
- Cancellation propagates through `AgentRuntimePort.cancel()` for recoverable team members before unfinished durable member state is marked cancelled.
- Restart evidence rehydrates spawned/running/waiting-approval members with durable thread/resume identity.
- Evaluator aggregation is deterministic arithmetic over typed evaluation records; M7 does not invent learning policy or silently promote agent behavior.

## M7 reuse gate
- REUSE / ADAPT integrated LangGraph durable execution only behind Nika `AgentRuntimePort`.
- REUSE M6 Pydantic `ToolGrant` rather than adding a second permission schema.
- REUSE the existing SQLite migration layer and `AuditLog`.
- CUSTOM (thin): Nika team identity, typed handoffs, lineage, quotas, privilege attenuation, evaluator evidence and cancellation coordination.
- Draft PR #11 was audited for reusable ideas but was not merged wholesale because it predates the current integrated M6 contracts and is non-mergeable against current main.

## Defects found and repaired during M7 acceptance
- Core CI run 146 reached real Ubuntu/Windows verification and found three Ruff defects: an unused import, import ordering and a too-broad `except Exception`. The branch removed the unused import, normalized imports and narrowed the modeled worker-failure boundary to `RuntimeError`; Ruff was not weakened.
- Core CI run 148 then passed dependency consistency, Ruff and compile, and executed the full pytest suite. It exposed two stale schema-version expectations in older M0/M1 regression tests (`5` instead of migration v6). Both expectations were updated, and the migration regression now also checks the four M7 tables.
- Exact-head Core CI run 150 passed dependency consistency, Ruff, compile and the complete pytest suite on both Ubuntu and Windows. Superseded/failed candidates were not credited.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. `AGENTS.md` references `docs/PARALLEL_DEVELOPMENT_POLICY.md`, but that file remains absent from `main` and exists only in stale PR #4; do not treat it as accepted canonical policy beyond parallel-first rules already present in integrated documents. Draft PR #11 remains uncredited except for ideas independently reviewed, reimplemented, proven and integrated through milestone-specific branches.

## Truth state
- M0–M7: IMPLEMENTED / GREEN / INTEGRATED.
- M5 has a diagnostic packaged acceptance proof only; this is not M11 release packaging.
- M8–M12: PREPARED/parallel work only unless separately evidenced.
- PACKAGED (release): false.
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false; automation must never award this state.

## Current weighted milestone
M8 — Self-learning & experiment engine.

## Next LARGE coherent batch
Build the largest safe M8 controlled-learning slice from the latest green `main`: versioned experiment/strategy definitions, immutable dataset/replay references, run/metric evidence, deterministic evaluators, champion/challenger comparison, explicit promotion/rollback gates and crash-safe persistence. Reuse existing runtime/model/memory/scheduler/resource contracts and maintained evaluation/optimization libraries only where they reduce glue without taking over Nika domain truth. Learning must occur in controlled experiments/simulations; it must never rewrite production source or silently widen permissions. Add deterministic replay, restart, regression, promotion-denial and rollback tests; integrate only after exact Ubuntu + Windows acceptance evidence is green. Keep M9–M12 independent lanes isolated and uncredited until their own gates.
