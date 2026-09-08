# Nika Core — current autonomous routing

EPOCH: EPOCH-0002
STATUS: ACTIVE
LAST_GLOBAL_AUDIT: bootstrap-required
NEXT_AUDIT_RULE: first available coordinator/worker performs immediate full audit; after that, first eligible worker after >=6h since last valid audit

## Mandatory first-run bootstrap audit

Before the coordinator establishes or changes worker directions, it MUST compare the newest durable project evidence and reconstruct the actual current Nika state. The first run is not allowed to assume that yesterday's worker prompts remain correct.

First-run audit must inspect, at minimum:

- current integrated product state;
- newest completed and still-active development work;
- current automated test/runtime evidence;
- current Windows/NVDA/release readiness;
- active ownership and collisions;
- latest durable GitHub reports/control files;
- relevant current Google Drive master reports when accessible;
- work completed by recent Work/Codex Cloud runs that may have invalidated old scheduled-worker assignments.

The coordinator classifies each worker assignment as:

- KEEP — still current and valuable;
- CHANGE — same lane, but next target changed;
- STOP_STALE — already completed, superseded or no longer useful;
- COLLISION — another worker/Codex already owns it;
- PROMOTE — newly critical because the product state changed.

Only after that comparison may the coordinator publish the new routing epoch.

## Operating rule

All recurring workers, Codex Cloud runs and Work integrators must read this file plus live project state before substantive work. Live project evidence overrides stale text here.

No worker may keep implementing an already-completed component merely because an old Scheduled Task prompt names it. Stable prompts define home lanes and startup procedure; this routing file defines the current target.

## Default worker lanes

- Worker 1: runtime/task continuity/offline-reconnect.
- Worker 2: ModelGateway/local/API integration.
- Worker 3: browser/batch/monitoring execution.
- Worker 4: accessibility/Windows/package/QA.
- Worker 5: integration/release/ownership; perform global routing audit only when >=6h has elapsed since the last valid audit, otherwise continue integration work.

These are stable home lanes, not permanent exact tasks. The coordinator may re-route when the V0.1 critical path changes.

## Current global priority

Finish the first genuinely usable Windows/NVDA V0.1. Avoid large unrelated feature expansion until the remaining end-to-end runtime, reconnect, batch/monitoring, packaging and human accessibility gates are closed.

When a major V0.1 blocker closes, immediately recompute worker priorities. Workers must move toward the next user-visible missing capability rather than polishing already-terminal infrastructure.

## Six-hour meta-audit without losing a worker

Do not dedicate one of five workers permanently to coordination.

Normal rule:

`if no valid full audit exists OR now - last_global_audit >= 6h OR major-change-trigger == true: perform audit + refresh routing; then continue productive project work`

Otherwise:

`continue normal lane work`

Major-change triggers include:

- a large Work or Codex Cloud package completes;
- a major integration changes the V0.1 critical path;
- Windows/NVDA/release readiness changes;
- a worker finds its assignment already complete;
- a collision or stale ownership is detected;
- a major runtime/CI blocker appears or closes;
- current routing contradicts live evidence.

If the normal coordinator has not refreshed routing for ~8 hours, the first capable worker may perform a failover audit.

## Coordination rule

If a lane is already complete, stale or actively owned elsewhere, re-route to the highest-value unowned blocker instead of continuing yesterday's assignment. Every worker must leave durable progress another worker can recover.

Before taking work, each worker checks current ownership. After taking work, it leaves/refreshes a current ownership marker/checkpoint. Ownership that stops producing evidence must eventually be treated as stale after verification.

## Codex Cloud rule

Codex Cloud must read this routing state and active worker ownership before substantive work.

At start it should:

1. reconstruct newest live Nika state;
2. identify what the scheduled workers are already expected to do during the next several hours;
3. choose a disjoint high-value package or intentional cross-lane integration package;
4. avoid consuming work already delegated to live scheduled workers unless intentionally taking over stale work;
5. leave durable checkpoints after major completed phases, not only at final shutdown.

When Codex completes a major package, it should refresh routing or leave enough durable evidence for the next coordinator to do so immediately. If credits end abruptly, scheduled workers recover from durable project state rather than waiting for Oleksii.

## Work / principal-auditor rule

A strong Work run is a periodic principal architect/integrator.

At beginning it reads this routing state and current product evidence. It may perform a deeper whole-project audit, correct architectural drift, update priorities, then take a difficult package that does not duplicate active workers.

A major Work result is itself a routing refresh trigger. Scheduled workers waking after it must re-read current evidence and must not continue assignments invalidated by the Work result.

## Coordinator output after every audit

A successful coordination audit must leave a compact durable state containing:

- new EPOCH;
- audit time;
- user-visible V0.1 status;
- what changed since previous audit;
- what is complete and must not be repeated;
- top remaining blockers in priority order;
- Worker 1–5 current targets;
- Codex Cloud active/next package;
- Work active/next package;
- active ownership/collisions;
- stale work to stop;
- integration/release queue;
- Windows/NVDA readiness;
- next audit rule;
- short owner-readable Ukrainian summary.

## Failover

If this file is older than the live project, reconstruct routing from current evidence and refresh it. If the normal coordinator misses its run, the first worker seeing routing older than ~8 hours may perform a minimal failover audit.

If Google Drive is unavailable but GitHub is reachable, continue from GitHub. Drive is a cross-account/master mirror, not a single point of failure.

## Core invariant

Oleksii must not have to manually enter a chat merely to say “read the latest reports and update the workers.” The autonomous system itself is responsible for comparing newest evidence, detecting stale directions, refreshing assignments and continuously moving Nika toward the first genuinely usable release.