# M3 — memory, scheduler and resource control

Status: IMPLEMENTED on development branch; acceptance credit requires exact green CI and integration.

## Reuse decision

Fresh upstream verification on 2026-08-18 confirms the binding M3 choices:

- **REUSE / ADAPT — APScheduler 3.11.x** behind Nika `SchedulerPort`. APScheduler supplies date/interval/cron trigger calculation and runtime execution. Nika does not implement a scheduling engine.
- **REUSE — psutil 7.x** behind `ResourceObserverPort` for cross-platform CPU and memory observation. Nika does not implement OS telemetry collection.
- **REUSE — SQLite** as authoritative local product state for memory records, schedule definitions and resource budgets.
- **CUSTOM (thin)** Nika memory scopes/consent/retention, stable schedule identity/action mapping and resource budget/fairness policy because these are product semantics that upstream engines cannot own.
- **DEFER — Qdrant/vector memory** until a measured semantic-retrieval evaluation demonstrates benefit. Transactional Nika memory remains SQLite-authoritative.

No third-party source is vendored. Framework-specific APScheduler objects remain inside the adapter.

## Durable memory contract

Namespaces are separated by `(scope, owner_id, namespace, key)`.

Scopes:
- task;
- agent;
- workspace;
- user-approved long-term memory.

User long-term memory fails closed unless the caller supplies explicit approval. Expiration is timezone-aware and deterministic; expired records are never returned and can be purged. Audit records identify the memory object and policy metadata without copying memory contents into the generic audit log.

## Scheduler contract

Nika persists serializable `ScheduledJob` definitions in SQLite. The persisted definition contains only stable product data: job ID, action ID, trigger kind/configuration, payload, enabled state and execution limits.

APScheduler uses an in-memory job store at runtime and is rehydrated from Nika's authoritative definitions on application start. This deliberately avoids making APScheduler pickle/job-store representation the product contract. Application callables are never persisted. At execution time the adapter resolves the stable `action_id` through a caller-supplied resolver.

Supported initial trigger classes are APScheduler's built-in date, interval and cron triggers. Pause disables the durable Nika definition; restart therefore does not resurrect a paused schedule. Resume reenables and reinstalls it.

## Resource control contract

`ResourceBudget` persists per `(scope, owner_id)` and can limit:
- concurrent work;
- observed CPU percentage;
- observed memory percentage.

`ResourceManager` applies deterministic FIFO ordering for waiting requests. The observer is replaceable; `PsutilResourceObserver` is the default system telemetry adapter.

Resource leases and waiting queues are intentionally process-local in this milestone. A process crash must release all operating-system work ownership rather than resurrect stale locks. Durable tasks/schedules may request resources again after restart; durable resource *budgets* remain authoritative in SQLite.

## Migration

Schema version 4 adds:
- `memory_records` plus scope/expiration indexes;
- `scheduled_jobs` plus enabled index;
- `resource_budgets`.

The normal ordered migration path remains mandatory and future-schema fail-closed behavior is unchanged.

## Acceptance evidence prepared

Automated tests cover:
- migration from the historical v1 fixture through schema v4;
- scoped memory persistence and cross-scope isolation;
- explicit approval requirement for user long-term memory;
- deterministic expiration and rejection of naive datetimes;
- schedule persistence and rehydration after constructing a new scheduler instance;
- pause surviving restart and explicit resume reinstalling the runtime job;
- invalid schedule contract rejection;
- durable resource budget reload;
- FIFO concurrency fairness;
- CPU and memory pressure blocking;
- cancellation of a waiting resource request.

M3 remains IMPLEMENTED, not GREEN or INTEGRATED, until the exact branch head passes the shared Ubuntu + Windows `scripts/verify.py` gate and the green candidate is merged.
