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
- Overall proven final A–Z product progress is **36.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed both Ubuntu and Windows jobs; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.

## M3 integrated capability
- schema migration v4 for memory records, scheduled-job definitions and resource budgets;
- durable task/agent/workspace/user memory scopes;
- explicit fail-closed approval requirement for user long-term memory;
- deterministic expiration/purge and scope isolation;
- framework-neutral `SchedulerPort` with APScheduler 3.11 adapter for date/interval/cron scheduling;
- SQLite-authoritative serializable schedule definitions and restart rehydration;
- durable pause/resume semantics with stable action-ID dispatch rather than persisted Python callables;
- persistent resource budgets, replaceable `ResourceObserverPort`, psutil observation and FIFO admission;
- CPU/RAM pressure limits, concurrency limits and cancellation of waiting requests;
- process-local resource leases by design so a crash does not resurrect stale ownership;
- regression evidence for migration, restart, expiration, approval, scheduling and resource fairness.

## M3 CI repair history
- Core CI run 96 reached real Ubuntu/Windows runners and first found a Ruff DTZ001 issue in the negative naive-datetime test; the test construction was repaired without weakening Ruff.
- Core CI run 97 passed dependency/Ruff/compile and exposed one stale pre-M3 assertion expecting schema version 3; the regression expectation was advanced to schema version 4.
- Core CI run 98 then passed the shared verification harness on both Ubuntu and Windows for exact head `c9c7e105838d9af8a65341fd28f4591aee0d851c`.

## M3 reuse gate
- REUSE/ADAPT APScheduler 3.11.x; Nika does not implement trigger/scheduling machinery.
- REUSE psutil 7.x for OS CPU/memory observation.
- REUSE SQLite for authoritative product state.
- CUSTOM (thin) only for Nika scope/consent/retention, stable schedule identity/action mapping, budgets and FIFO fairness.
- Qdrant/vector memory remains deferred until measured semantic-retrieval evidence justifies it.

## Governance consistency issue discovered this cycle
Repository metadata is currently PUBLIC despite older wording referring to a private repository.
PR #4 `Governance: parallel-first development across independent Nika workstreams` is currently OPEN, not merged, and GitHub reports it non-mergeable. `docs/PARALLEL_DEVELOPMENT_POLICY.md` is absent from the latest green baseline that M3 branched from, so older status text claiming PR #4 was merged was incorrect. The active parallel-first rules are nevertheless present in `AGENTS.md`, `docs/MASTER_SPEC.md`, `docs/ROADMAP.md` and `state/PARALLEL_EXECUTION_BOARD.md`. PR #4 requires separate cleanup/reconciliation and is not counted as integrated evidence.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4–M12: PREPARED/parallel lanes only unless separately evidenced.
- No Windows standalone package yet.
- No human NVDA verification yet.

## Current milestone
M4 — Model Gateway + standardized tools + MCP integration is the next weighted milestone. Parallel M5–M12 lanes may advance where independent, but product weight remains acceptance-gated.

## Next LARGE coherent batch
Build the M4 provider-neutral model/tool execution slice from the latest green `main`: provider capability contracts and routing, explicit local/cloud/no-LLM selection, standardized tool call envelopes, timeout/cancellation/audit, secrets-safe configuration, MCP client/server boundary and deterministic mock/offline proofs. Re-check current LiteLLM and MCP official APIs before implementation, keep provider/runtime types behind adapters, and run the full Ubuntu + Windows gate before any M4 credit.
