# Nika Core — autonomous worker orchestration control plane

## Purpose

This document defines how recurring scheduled workers, long Codex Cloud runs and occasional high-capability Work runs cooperate without requiring Oleksii to manually rewrite worker prompts after every development wave.

The design goal is **five productive development workers plus coordination as an intermittent duty**, not four developers plus one permanently dedicated coordinator.

## Stable prompts, live routing

Recurring worker prompts should remain stable. Their assignment is resolved at runtime from:

- `coordination/AUTONOMOUS_ROUTING.md`;
- live main/project state, tests, open work and ownership;
- current durable project reports;
- Drive mirror for owner-readable cross-account continuity.

If a static prompt conflicts with newer project state, the newer live state wins. A worker must not keep implementing an already-completed component merely because yesterday’s prompt named it.

## Coordination duty without losing a worker

All five workers remain developers.

Worker 5 / Integrator carries a meta-coordination duty only when due. On normal runs it continues real integration/product work.

Recommended rule:

`if now - last_global_audit >= 6 hours: audit + refresh routing; then continue normal work`

On all other runs:

`continue normal lane work`

This avoids a permanent 20% resource tax.

## Elapsed-time scheduling, not exact clock slots

Do not rely exclusively on exact times such as 06:00/12:00/18:00/00:00. A missed run, credit exhaustion or schedule delay would create a coordination hole.

The shared routing file records the last successful global audit. The first eligible coordinator run after six elapsed hours performs the audit.

If the designated coordinator has not refreshed routing after a larger failover threshold (recommended 8 hours), another worker may perform a minimal failover audit before taking its own lane.

## Every worker startup

Each scheduled run should:

1. read `coordination/AUTONOMOUS_ROUTING.md`;
2. inspect live project state relevant to its lane;
3. verify the assignment remains unfinished and unowned elsewhere;
4. refresh or claim a time-bounded ownership lease;
5. perform real product work;
6. persist results and test state;
7. leave a concise durable lane checkpoint.

If the old task is already done, immediately re-route instead of spending a run repeating it.

## Ownership leases

Each active package should record:

- worker identity;
- scope;
- last refresh time;
- expected next checkpoint;
- expiry;
- state: ACTIVE / BLOCKED / READY_TO_INTEGRATE / TERMINAL / RELEASED.

Expired ownership is not automatically free: first check whether another live worker is still producing evidence.

## Codex Cloud

A long Codex Cloud run should act as an additional high-throughput implementer/integrator while respecting scheduled-worker ownership.

At start:

1. read routing and current live state;
2. identify packages already delegated to scheduled workers;
3. choose a disjoint high-value package;
4. publish its temporary ownership.

Before a natural stop:

1. persist product progress;
2. refresh routing with changed priorities;
3. release/transfer ownership;
4. identify useful next packages for scheduled workers.

If Codex stops abruptly from usage limits, scheduled workers recover from durable state; they must not depend on a final Codex message.

## Work / principal integrator

A strong Work run may periodically perform a deeper whole-project audit, integrate large cross-lane changes, repair architectural drift and refresh routing. It then takes a disjoint hard package rather than duplicating the scheduled workers.

## Routing refresh triggers

Refresh the shared plan when:

- >=6 hours elapsed since last global audit;
- a major V0.1 blocker closes;
- integration substantially changes the critical path;
- a worker collision/stale assignment is found;
- Codex Cloud or Work completes a major package;
- release/NVDA/Windows readiness changes;
- the routing file contradicts live state.

## Recommended five-worker shape for Nika V0.1

The exact assignments may change dynamically, but a useful default is:

1. runtime/task continuity/offline-reconnect;
2. ModelGateway/local/API integration;
3. browser/batch/monitoring execution;
4. accessibility/Windows/package/QA;
5. integration/release/ownership + six-hour meta-coordination duty.

Worker 5 remains a real integration/release developer between audits.

## Development epoch

Every routing refresh increments an epoch such as `EPOCH-0008`.

Workers record the epoch they read. If a newer epoch appears, they must re-evaluate their assignment before continuing stale work.

## Shared routing contents

The routing document should contain:

- epoch;
- generated/last-audit time;
- current user-visible product status;
- top blockers;
- worker 1–5 assignments;
- ownership leases;
- Codex/Work active package if any;
- integration/release queue;
- stale work to stop;
- next audit rule;
- short owner-readable summary.

## Key invariant

**Do not coordinate by continually rewriting five task prompts. Coordinate through one live routing authority that every worker reads and updates from current evidence.**

This lets scheduled tasks, Codex Cloud and Work advance at different speeds without forcing Oleksii to manually reprogram the workforce.