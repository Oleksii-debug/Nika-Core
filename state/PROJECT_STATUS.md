# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT / PARALLEL-FIRST
Repository visibility observed this cycle: PUBLIC.

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- M2 durable agent runtime: GREEN / INTEGRATED, 100% of its 11% weight.
- M3 memory/scheduler/resource control: IMPLEMENTED on development branch; no weight credited until exact green CI + merge.
- Overall proven final A–Z product progress remains **27.0%**.

## Proven foundation
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- Current `main` observed at cycle start: `58a5fbf708493b64cb76ed6541928e2f17ae6bc8`, the merged full reuse-first architecture audit.

## M3 current implementation
Branch: `dev/m3-memory-scheduler-resources`.
Implementation baseline before status-only synchronization: `ac57ec1524ba8e4efec944e3ef3acd12ee865e81`.
State: IMPLEMENTED / CI NOT YET PROVEN / NOT INTEGRATED / NOT PACKAGED / NOT HUMAN_TESTED.

Implemented coherent slice:
- schema migration v4 for memory records, scheduled-job definitions and resource budgets;
- versioned Nika memory scopes: task, agent, workspace and user-approved long-term memory;
- explicit fail-closed approval requirement for user long-term memory;
- deterministic expiration/purge and scope isolation;
- `SchedulerPort` plus APScheduler 3.11 adapter using date/interval/cron triggers;
- SQLite-authoritative serializable schedule definitions with runtime rehydration after restart;
- persisted pause/resume semantics and stable action-ID dispatch rather than persisted Python callables;
- resource budget persistence, replaceable `ResourceObserverPort`, psutil adapter and deterministic FIFO admission;
- CPU/RAM pressure limits, concurrency limits and waiting-request cancellation;
- regression/acceptance tests covering migration, restart, expiration, approval, scheduling and resource fairness;
- reuse/evidence document `docs/M3_MEMORY_SCHEDULER_RESOURCES.md`.

## M3 reuse gate
- REUSE/ADAPT APScheduler 3.11.x; Nika does not implement trigger/scheduling machinery.
- REUSE psutil 7.x for OS CPU/memory observation.
- REUSE SQLite for authoritative product state.
- CUSTOM (thin) only for Nika scope/consent/retention, stable schedule identity/action mapping, budgets and FIFO fairness.
- Qdrant/vector memory remains deferred until measured semantic-retrieval evidence justifies it.

## M3 unverified / next gate
No M3 test is claimed passed yet on the branch until GitHub Actions executes the exact PR head on Ubuntu and Windows. Required next sequence:
1. open/update M3 PR;
2. execute shared `python scripts/verify.py` through Core CI on both operating systems;
3. repair real failures without weakening checks;
4. merge only the exact green head;
5. only then award M3's 9% milestone weight.

## Governance consistency issue discovered this cycle
Repository metadata is currently PUBLIC despite older user wording referring to a private repository.
PR #4 `Governance: parallel-first development across independent Nika workstreams` is currently OPEN, not merged, and GitHub reports it non-mergeable. `docs/PARALLEL_DEVELOPMENT_POLICY.md` is absent from current `main`, so older status text claiming PR #4 was merged was incorrect. The active parallel-first rules are nevertheless present in `AGENTS.md`, `docs/MASTER_SPEC.md`, `docs/ROADMAP.md` and `state/PARALLEL_EXECUTION_BOARD.md`. PR #4 requires separate cleanup/reconciliation and is not counted as integrated evidence.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED on `dev/m3-memory-scheduler-resources`; not yet GREEN or INTEGRATED.
- M4–M12: PREPARED/parallel lanes only unless separately evidenced.
- No Windows standalone package yet.
- No human NVDA verification yet.

## Next LARGE coherent batch
Priority is to close the M3 acceptance gate on the current coherent slice: inspect exact CI, fix source/test/migration/restart defects, merge only green, then update M3 credit. Independent M4–M12 work may continue only where it does not interfere with this gate.
