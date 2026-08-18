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
- M8 self-learning & experiment engine: GREEN / INTEGRATED, 100% of its 10% weight.
- Overall proven final A–Z product progress is **82.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI run 112 passed Ubuntu, Windows and focused live Ollama proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 exact green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI run 137 passed Ubuntu, Windows and packaged WebView2/UIA keyboard-focus proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
- M6 exact green head `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI run 142 passed Ubuntu and Windows; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
- M7 exact green head `4feb976faa97949bacc321bcbce792d01359a58c`; Core CI run 150 passed Ubuntu and Windows; PR #17 merged as `5a01692c1372375f040cd38558e33204b082d5a5`.
- M8 exact green head `d8f14ac097f2eb664b7f893543db359fb6245e6c`; Core CI run 159 passed Ubuntu and Windows; PR #19 merged as `a8fd7b6c094489cebb79a4221e4124b5cfbad8f6`.

## M8 integrated evidence
- Versioned immutable experiment definitions bind champion/challengers to prompt/strategy/config artifacts, fixed replay/dataset references, explicit metrics and promotion policy.
- Permission fingerprints are invariant across champion and challengers; M8 cannot silently widen permissions.
- Metric observations reject unknown candidates/replays/metrics, duplicate evidence and non-finite values.
- Deterministic evaluation requires complete fixed-replay coverage for the primary metric and guardrails before promotion can occur.
- Champion/challenger promotion uses an explicit minimum primary-metric improvement and maximum guardrail regression; deterministic tie-breaking is recorded.
- Rollback is legal only after a recorded promotion and restores the recorded previous champion.
- SQLite schema v7 durably stores canonical experiment definitions, append-only observations and append-only lifecycle/promotion/rollback events.
- `SQLiteExperimentRepository` keeps definitions and recorded evidence immutable, rejects stale writers that would drop evidence, and narrows legal state transitions independently of the higher-level engine.
- Lifecycle state plus promotion/rollback event evidence are written in one local SQLite transaction. Fault-injection proves an event-write failure rolls the state transition back.
- Process-recreation proof resumes a running experiment from the same database, completes/promotion after restart, recreates again and rolls back from durable evidence.
- M8 exposes no production-source mutation path. Learning remains controlled evaluation/simulation and does not authorize external/high-impact actions or bypass existing R4 approvals.

## M8 reuse gate
- REUSE current Nika runtime/model/memory/scheduler/resource boundaries and the canonical SQLite migration/transaction layer.
- REUSE deterministic Python arithmetic/statistics for baseline evaluator behavior instead of introducing a broad optimization framework for bookkeeping.
- ADAPT DSPy only for future experiments with explicit datasets/metrics where it materially reduces optimization glue.
- ADAPT Gymnasium only for controlled simulation/RL environments that require its environment contract.
- CUSTOM (thin): Nika experiment identity, immutable replay evidence, permission invariance, promotion/rollback policy and repository adapter because these are product/safety semantics.

## Defects found and repaired during M8 acceptance
- Initial M8 foundation head `1bb6ad4090635bcfb1441ca2ab865c87e37f3c10` passed Core CI 157 but was deliberately not credited because durable persistence/crash-restart evidence was absent.
- Durable candidate `8456409658a45c045306406db5716264b42195d3` reached real Ubuntu/Windows verification in Core CI 158. Dependency consistency, Ruff and compile passed; pytest reported 126 passing tests and one stale M6 regression expecting schema v6 instead of v7. The acceptance gate was not weakened and the failed candidate received no credit.
- Exact repaired head `d8f14ac097f2eb664b7f893543db359fb6245e6c` passed complete Core CI 159 on Ubuntu and Windows, including the schema v7 migration, durable restart/promotion/rollback path, append-only/stale-writer protections and transaction fault-injection tests.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. `AGENTS.md` references `docs/PARALLEL_DEVELOPMENT_POLICY.md`, but that file remains absent from `main` and exists only in stale PR #4; do not treat it as accepted canonical policy beyond parallel-first rules already present in integrated documents. Draft PR #11 remains uncredited except for ideas independently reviewed, reimplemented, proven and integrated through milestone-specific branches.

## Truth state
- M0–M8: IMPLEMENTED / GREEN / INTEGRATED.
- M5 has a diagnostic packaged acceptance proof only; this is not M11 release packaging.
- M9–M12: PREPARED/parallel work only unless separately evidenced.
- PACKAGED (release): false.
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false; automation must never award this state.

## Current weighted milestone
M9 — Plugin SDK & real workspaces.

## Next LARGE coherent batch
Build the largest safe M9 plugin/workspace slice from the latest green `main`: versioned plugin/workspace manifests and compatibility contracts, capability/permission declarations, entry-point discovery/activation, stable SDK boundaries, migration/version checks and isolated real-workspace proofs. Prioritize the product emphasis by proving at least the Software Factory and Accessibility Repair/Assistant workspaces behind reusable ports: official API/SDK/CLI adapters for coding workers, Playwright/DOM and Windows UI Automation semantic interaction before vision fallback, sandbox/branch/test gates for generated code, and auditable permission/cancellation boundaries. Recheck current upstream versions/licenses immediately before adoption. Keep M10–M12 independent lanes isolated and uncredited until their own acceptance gates are green and integrated.
