# Resource capacity telemetry

Status: candidate infrastructure for the continuous Full Product runtime. It does not change the V0.1 admission policy and does not create a second resource manager.

## Reuse decision

- **REUSE** the existing `ResourceManager`, durable `ResourceBudget`, FIFO/concurrency admission and SQLite budget authority.
- **REUSE** psutil behind `ResourceObserverPort`; no custom OS telemetry collector.
- **ADAPT** the existing `ResourceSnapshot` with optional logical-CPU, total-memory, current-process RSS and battery/power measurements.
- **CUSTOM (thin)** `ResourceCapacityStatus` and `ResourceManager.status()` as a deterministic read-only projection of current budget, active/queued work and measured headroom.

No scheduler, background thread, watchdog, retry engine, database migration or new policy authority is introduced.

## Contract

`ResourceManager.status(scope, owner_id)`:

- reads the persisted existing budget;
- samples the configured observer once;
- reports active and queued counts without mutating them;
- reports remaining concurrency capacity;
- reports CPU/memory headroom against the existing owner budget when such limits exist;
- reports the same pressure reason names used by admission: `concurrency_limit`, `cpu_limit`, `memory_limit`;
- exposes optional host/process/power measurements only when the operating system reports them.

Negative CPU or memory headroom means the measured value is already above the configured budget. Missing optional telemetry stays `None`; Nika does not invent hardware or battery facts.

## Why this survives checkpoints

Future background-life and autonomy policy can consume one stable read-only capacity projection while actual work admission continues through the same `ResourceManager.request()` authority. Resource profiles such as economy/night/heavy-batch are intentionally not invented here; profile thresholds require measured product evidence and user/policy decisions.

## Evidence boundary

Automated tests use deterministic fake measurements and monkeypatched psutil calls. They prove contract normalization and no-mutation behavior only. They are not physical-laptop performance evidence and do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
