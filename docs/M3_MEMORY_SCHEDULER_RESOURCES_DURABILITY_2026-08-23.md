# DEV22 Memory, Scheduler, and Resource Durability Hardening — 2026-08-23

## Scope

This batch extends the integrated M3 implementation without replacing its scheduler, storage, or
resource-observation stack. It owns only Nika memory retention/scopes, SchedulerPort persistence
identity, resource budgets/fairness/recovery, and directly coupled SQLite schema.

Starting live main for the run was `bd7517f38c04560aa7350b870d8a51bfb6c8113b`. Before the first
write, main advanced through the collision-free PF2 lease-identity merge to
`e40691a6e2ff9c31fd413f63d004612e048d95ed`; this branch was therefore rooted at the newer SHA.

## REUSE -> ADAPT -> CUSTOM(thin)

### REUSE

- APScheduler 3.x remains the sole production scheduling engine behind `SchedulerPort`.
  The repository range `apscheduler>=3.11,<4` includes current stable APScheduler 3.11.3.
- psutil remains the resource-observation adapter. The repository range `psutil>=7,<8` includes
  psutil 7.2.2.
- SQLite remains authoritative transactional truth for memory, schedule definitions, resource
  budgets, durable resource requests, and restart identity.
- Existing `scheduled_jobs`, `resource_budgets`, `memory_records`, audit primitives, and public
  M3 APIs remain compatible.

### ADAPT

- APScheduler receives Nika-owned durable `ScheduleIdentity` values and continues to rehydrate
  jobs into one UTC `BackgroundScheduler`; no competing scheduler or APScheduler job-store schema
  is introduced.
- psutil observation now includes disk usage and current-process RSS. GPU observation remains
  explicitly unavailable in the psutil adapter because psutil does not expose portable GPU
  utilization; a GPU budget fails closed unless another `ResourceObserverPort` supplies it.
- The first psutil CPU sample uses a bounded 0.1-second interval because psutil documents the
  first non-blocking `cpu_percent()` sample as meaningless.

### CUSTOM(thin)

- Nika memory scopes/retention and long-term user-consent policy.
- Stable ProductProject-aware schedule binding and owner-local schedule dedup key.
- Durable FIFO resource request identities and exact stale lease-owner recovery.
- Nika-specific fail-closed budget decisions for unsupported or invalid observations.

No vector database, new scheduler, new ORM, GPU library, or model dependency is added.

## Memory contract

`MemoryScope` now covers:

- `short_term`;
- `task`;
- `thread`;
- `agent`;
- `workspace`;
- user-approved long-term `user` (`USER_LONG_TERM`, with `USER` retained as the compatibility
  alias).

The SQLite primary key remains `(scope, owner_id, namespace, memory_key)`, so the same owner,
namespace, and key cannot leak across scopes. Long-term user memory still requires explicit
approval on every write.

`MemoryRetentionPolicy` supports persisted TTL and deterministic `max_records` trimming within the
exact scope/owner/namespace. Invalid Boolean, fractional, zero, or negative retention numbers fail
closed. Trimming keeps the just-written record and the most recently updated remaining records.
Expired records are removed before the cardinality decision.

## Scheduler contract

`ScheduleIdentity` contains:

- `scope`;
- `owner_id`;
- owner-local `dedup_key`;
- optional `product_project_id`.

The binding is durable in SQLite and survives adapter/process restart. Once attached to a
`job_id`, it cannot be silently cleared or rebound. A second job cannot claim the same
`(scope, owner_id, dedup_key)`.

This is definition-level deduplication and stable ownership identity. It is not a claim of
exactly-once external side effects. Action implementations remain responsible for their normal
idempotency/reconciliation boundary.

Date trigger `run_date` and any provided interval/cron `start_date` or `end_date` must be
timezone-aware ISO-8601 strings before persistence. Runtime scheduling remains APScheduler in UTC.

## Resource contract

`ResourceBudget` keeps the existing concurrent/CPU/RAM ceilings and adds optional:

- disk utilization ceiling;
- GPU utilization ceiling;
- current-process RSS byte ceiling.

`ResourceSnapshot` adds measured disk utilization/free bytes and process RSS, plus optional GPU
utilization. Unsupported requested observations fail closed instead of being treated as zero.

Resource requests are now durable FIFO records with stable
`(scope, owner_id, request_id, product_project_id)` identity. SQLite `BEGIN IMMEDIATE` serializes
grant decisions across concurrent managers, while an in-process `RLock` serializes threads.
Terminal requests that are requested again receive a new queue position instead of jumping ahead
of existing waiters.

A process restart does not silently treat a stale durable grant as free. A new manager first sees
`recovery_required`; the caller can inspect `stale_lease_owners()` and explicitly release one
verified dead owner with `recover_after_restart(stale_manager_id=...)`. Recovery never releases
leases belonging to another owner ID. Waiting requests and ProductProject identity survive.

## Schema compatibility decision

The global core migration stream is already shared with active research work through schema 13.
To avoid editing that shared stream during parallel manual lanes, this batch adds one additive,
versioned M3 extension migration stream inside the existing `SQLiteStore` migration mechanism.
This is not a second persistence framework: it uses the same SQLite connection, transaction,
ordered-version, and future-schema fail-closed pattern already used by ProductProject storage.

M3 extension schema v1:

- adds optional disk/GPU/process-RSS columns to `resource_budgets`;
- adds `scheduled_job_bindings`;
- adds `resource_requests` and FIFO/lease-owner indexes.

Existing databases migrate in place. Fresh databases first receive the existing core schema and
then the M3 extension. A future unsupported M3 extension version fails closed.

## Security and failure boundaries

- No permission, tool, network, filesystem, or R0-R4 approval grant is expanded by this batch.
- Resource budgets constrain execution; they do not authorize an action.
- Schedule identity does not bypass Action Registry, ToolExecutor, or approval checks.
- Memory values are not copied into audit payloads.
- GPU capacity is not guessed from vendor/framework state.
- Vector stores, if later justified for retrieval quality, remain derived indexes and cannot
  become scheduler/resource/task truth.
- No HUMAN_TESTED or NVDA_VERIFIED credit is produced by automated tests.

## Qualification

Focused regressions cover:

- six memory scopes with identical names/keys and restart isolation;
- deterministic TTL/cardinality retention;
- user long-term approval;
- ProductProject schedule identity restart, dedup, immutable binding, timezone validation, and
  pause/resume preservation;
- M3 extension future-schema rejection;
- extended resource budget persistence;
- crash/restart stale-grant release with FIFO waiters;
- ProductProject resource identity drift rejection;
- unsupported disk/GPU/process observation fail-closed behavior;
- concurrent grant serialization;
- non-finite observation rejection;
- live psutil CPU/RAM/disk/process-RSS contract.

Exact repository acceptance remains Ruff, compile/import, focused tests, full pytest on Ubuntu and
Windows through GitHub Actions. Only an exact green candidate head receives acceptance credit.
