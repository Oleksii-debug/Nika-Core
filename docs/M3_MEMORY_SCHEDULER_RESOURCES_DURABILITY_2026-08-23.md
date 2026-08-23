# DEV22 Memory, Scheduler, and Resource Durability Hardening — 2026-08-23

## Scope

This batch extends the integrated M3 implementation without replacing its scheduler, storage, or
resource-observation stack. It owns only Nika memory retention/scopes, SchedulerPort persistence
identity, resource budgets/fairness/recovery, and directly coupled SQLite schema.

Starting live main for the run was `bd7517f38c04560aa7350b870d8a51bfb6c8113b`. Before the first
write, main advanced through the collision-free PF2 lease-identity merge to
`e40691a6e2ff9c31fd413f63d004612e048d95ed`; this branch was rooted at the newer SHA.

## REUSE -> ADAPT -> CUSTOM(thin)

### REUSE

- APScheduler 3.x remains the sole production scheduling engine behind `SchedulerPort`.
  Exact CI resolved APScheduler 3.11.3 on the initial DEV22 candidate.
- psutil remains the resource-observation and local process-liveness adapter. Exact CI resolved
  psutil 7.2.2 on the initial DEV22 candidate.
- SQLite remains authoritative transactional truth for memory, schedule definitions, resource
  budgets, durable resource requests, and restart identity.
- Existing `scheduled_jobs`, `resource_budgets`, `memory_records`, audit primitives, and public
  M3 APIs remain compatible.

### ADAPT

- APScheduler receives Nika-owned durable `ScheduleIdentity` values and continues to rehydrate
  jobs into one UTC `BackgroundScheduler`; no competing scheduler or APScheduler job-store schema
  is introduced.
- psutil observation includes disk usage and current-process RSS. GPU observation remains
  explicitly unavailable because psutil has no portable GPU utilization API.
- psutil also supplies process PID plus process creation time for independent lease-owner liveness
  checks. This prevents a caller-supplied manager ID from being treated as proof that an owner died.
- The first psutil CPU sample uses a bounded 0.1-second interval because the first non-blocking
  `cpu_percent()` sample is not useful.

### CUSTOM(thin)

- Nika memory scopes/retention and long-term user-consent policy.
- Stable ProductProject-aware schedule binding and owner-local schedule dedup key.
- Durable FIFO resource request identity and process-generation-bound crash recovery.
- Nika-specific fail-closed budget decisions for unsupported or invalid observations.

No vector database, scheduler engine, ORM, GPU library, heartbeat service, or model dependency is
added.

## Memory contract

`MemoryScope` covers:

- `short_term`;
- `task`;
- `thread`;
- `agent`;
- `workspace`;
- user-approved long-term `user` (`USER_LONG_TERM`, with `USER` retained as a compatibility alias).

The SQLite primary key remains `(scope, owner_id, namespace, memory_key)`, so equal names and keys
cannot leak across scopes. Long-term user memory still requires explicit approval on every write.

`MemoryRetentionPolicy` supports persisted TTL and deterministic `max_records` trimming within the
exact scope/owner/namespace. Boolean, fractional, zero, or negative retention values fail closed.
Expired records are removed before the cardinality decision.

## Scheduler contract

`ScheduleIdentity` contains:

- `scope`;
- `owner_id`;
- owner-local `dedup_key`;
- optional `product_project_id`.

The binding is durable in SQLite and survives adapter/process restart. Once attached to a `job_id`,
it cannot be silently cleared or rebound. A second job cannot claim the same
`(scope, owner_id, dedup_key)`.

This is definition-level deduplication and stable ownership identity. It is not an exactly-once
external-side-effect claim. Action implementations retain their normal idempotency/reconciliation
boundary.

Date trigger `run_date` and any provided interval/cron `start_date` or `end_date` must be
timezone-aware ISO-8601 strings before persistence. Runtime scheduling remains APScheduler in UTC.

## Resource contract

`ResourceBudget` keeps the concurrent/CPU/RAM ceilings and adds optional disk utilization, GPU
utilization, and current-process RSS ceilings. Unsupported requested observations fail closed.

Resource requests are durable FIFO records with stable
`(scope, owner_id, request_id, product_project_id)` identity. SQLite `BEGIN IMMEDIATE` serializes
grant decisions across concurrent managers, while an in-process `RLock` serializes threads.
Terminal requests that are requested again receive a new queue position instead of jumping ahead
of existing waiters.

A manager instance tracks only grants it actually acquired. Reusing a textual `manager_id` in a new
instance cannot adopt or release an older instance's lease, and it cannot acquire new leases while
unreconciled grants already use that manager ID.

### Crash recovery authority

A durable grant records, when available, the owner process PID and process creation timestamp.
`stale_manager_id` is only a selector; it is never authority that the selected owner is stale.

`recover_after_restart()` requires an independent `ResourceOwnerProbePort` and fails closed when:

- no liveness probe is available;
- process identity is missing or corrupt;
- one manager ID spans multiple unresolved process generations;
- the exact PID + creation-time process is still alive;
- durable identity changes during the recovery transaction.

Only after the probe proves that exact process generation is dead may SQLite transition its grants
to `released_after_restart`. `PsutilResourceObserver` provides the local implementation. PID alone
is insufficient because operating systems may reuse PIDs; creation time is part of the identity.
`AccessDenied` is treated as alive because inability to inspect a process is not proof of death.

`stale_lease_owners()` is retained for compatibility but returns recovery candidates only. Its
result does not certify liveness or authorize release.

## Schema compatibility decision

The global core migration stream is shared with active research work through schema 13. This batch
uses one additive, versioned M3 extension stream inside the existing `SQLiteStore` migration
mechanism. It is not a second persistence framework: it uses the same SQLite connection,
transaction, ordered-version, and future-schema fail-closed pattern.

M3 extension schema v1:

- adds optional disk/GPU/process-RSS columns to `resource_budgets`;
- adds `scheduled_job_bindings`;
- adds `resource_requests` and FIFO/lease-owner indexes.

M3 extension schema v2 adds nullable process PID and process-start identity to durable resource
leases. A legacy or corrupt grant without verifiable process identity is preserved but cannot be
force-released by restart recovery; it fails closed for operator/reconciliation handling.

## Security and failure boundaries

- No permission, tool, network, filesystem, or R0-R4 approval grant is expanded.
- Resource budgets constrain execution; they do not authorize an action.
- Schedule identity does not bypass Action Registry, ToolExecutor, or approval checks.
- Memory values are not copied into audit payloads.
- GPU capacity is not guessed from vendor/framework state.
- A caller cannot nominate another live manager and free its lease merely by knowing its ID.
- Vector stores, if later justified, remain derived indexes and cannot become scheduler/resource
  or task truth.
- No HUMAN_TESTED or NVDA_VERIFIED credit is produced by automated tests.

## Qualification

Focused regressions cover:

- six memory scopes with restart isolation and deterministic retention;
- ProductProject schedule identity restart/dedup/immutable binding/timezone validation;
- M3 extension future-schema rejection;
- extended resource budget persistence;
- proven-dead process recovery with FIFO continuity;
- the independent AUD03 live-owner attack: live owner recovery must fail closed;
- missing-liveness-proof recovery failure;
- manager-ID reuse cannot adopt/release an older lease;
- corrupt owner-process identity failure;
- ProductProject resource identity drift rejection;
- unsupported disk/GPU/process observations;
- concurrent grant serialization and non-finite observation rejection;
- live psutil CPU/RAM/disk/process-RSS and process-liveness contract.

Exact repository acceptance remains dependency consistency, Ruff, compile/import, full pytest on
Ubuntu and Windows, plus independent AUD03 replay against the exact repaired candidate. Only an
exact green candidate head receives acceptance credit.
