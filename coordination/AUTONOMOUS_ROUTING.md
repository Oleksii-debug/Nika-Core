# Nika Core — current autonomous routing

EPOCH: EPOCH-0001
STATUS: ACTIVE
LAST_GLOBAL_AUDIT: bootstrap-required
NEXT_AUDIT_RULE: first eligible worker after >=6h since last valid audit

## Operating rule

All recurring workers, Codex Cloud runs and Work integrators must read this file plus live project state before substantive work. Live project evidence overrides stale text here.

## Default worker lanes

- Worker 1: runtime/task continuity/offline-reconnect.
- Worker 2: ModelGateway/local/API integration.
- Worker 3: browser/batch/monitoring execution.
- Worker 4: accessibility/Windows/package/QA.
- Worker 5: integration/release/ownership; perform global routing audit only when >=6h has elapsed since the last valid audit, otherwise continue integration work.

## Current global priority

Finish the first genuinely usable Windows/NVDA V0.1. Avoid large unrelated feature expansion until the remaining end-to-end runtime, reconnect, batch/monitoring, packaging and human accessibility gates are closed.

## Coordination rule

If a lane is already complete, stale or actively owned elsewhere, re-route to the highest-value unowned blocker instead of continuing yesterday’s assignment. Every worker must leave durable progress another worker can recover.

## Codex Cloud rule

Codex Cloud reads active worker ownership, claims a disjoint package, avoids duplicating the next scheduled-worker window, and refreshes this routing document before a natural stopping point when possible.

## Failover

If this file is older than the live project, reconstruct routing from current evidence and refresh it. If the normal coordinator misses its run, the first worker seeing routing older than ~8 hours may perform a minimal failover audit.
