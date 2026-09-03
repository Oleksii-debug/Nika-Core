# V0.1 durable recurrence

Status: implementation candidate only. Acceptance credit requires exact-head Core/M12, independent
audit, compatibility with the canonical M3 scheduler owner, and guarded integration.

## User journey

V0.1 must support: “check every N minutes until a deadline or condition” while preserving the same
durable intent across close/reopen, pause/resume, and cancellation.

## Reuse / adapt / custom decision

- **REUSE:** the existing `SchedulerPort`, `APSchedulerAdapter`, `ScheduledJobStore`, and SQLite
  `scheduled_jobs` authority.
- **ADAPT:** represent a recurrence as one durable APScheduler date-trigger intent at a time. The
  next intent and completion cursor live inside the already-persisted `ScheduledJob` payload.
- **CUSTOM (thin):** V0.1 recurrence lifecycle, fixed-interval next-intent arithmetic, the explicit
  missed-run policy, and stable occurrence identity.
- **NO NEW SCHEDULER:** APScheduler still owns runtime wake-up/execution. This layer does not create
  a scheduler thread, process-local sleep loop, job store, or schema migration.

The additive module intentionally does not edit the four scheduler files owned by M3 successor
PR #357. It also does not edit ToolExecutor/idempotency code owned by the durable-effect lane.

## Durable state

Every recurrence has a deterministic scheduler job ID derived from `recurrence_id`. Its persisted
payload contains:

- immutable recurrence ID and target action ID;
- fixed interval and UTC anchor;
- optional UTC deadline;
- lifecycle state;
- explicit missed-run policy;
- exact next due timestamp plus stable next occurrence ID;
- exact last-completed timestamp plus stable completed occurrence ID;
- terminal reason when a condition or deadline ends the series;
- target payload.

A new service instance backed by the same `ScheduledJobStore` reconstructs that state without a
process-local timer or caller-supplied cursor.

## Missed-run policy

V0.1 policy is `COALESCE_ONE`.

If Nika is offline or paused while one or many slots pass, the persisted next intent remains the
single catch-up occurrence. It uses `misfire_grace_seconds=None`, so APScheduler may execute that one
durable date intent after restart. After it completes, intermediate missed slots are skipped and the
service persists the first fixed-interval slot strictly after the current clock. This prevents a
catch-up storm.

Resume uses the same rule: an overdue persisted intent is retained as the one coalesced occurrence;
a future intent is not moved earlier.

## Pause, cancel, condition, deadline

- Pause persists `PAUSED` and disables the scheduler job. Repeated pause is idempotent.
- Resume is explicit. It reenables the same next durable intent unless the deadline has expired.
- Cancel persists `CANCELLED`, clears the next intent, and leaves the durable terminal record
  disabled. Repeated cancel is idempotent.
- A handler can return `RecurrenceDecision.STOP`; the recurrence becomes `COMPLETED` with
  `CONDITION_MET`.
- No handler is started at or after the deadline. A future slot at or beyond the deadline is never
  persisted as executable.

## Per-occurrence identity and external effects

Occurrence identity is deterministic from `(recurrence_id, scheduled_for_utc)` and is passed to the
target as `RecurrenceInvocation.occurrence_id`. It is stable across restart and changes only when
the next intent advances.

This is an identity for the canonical durable ToolExecutor effect boundary; it is not a second
effect ledger. If a process dies after an external effect but before recurrence completion is
persisted, the same occurrence identity is replayed so the durable effect ledger can return the
recorded completion or fail closed on uncertainty instead of blindly repeating the external
effect.

## Deterministic verification

`tests/test_v01_durable_recurrence.py` uses an injected clock and a persistence-only SchedulerPort
test adapter. It contains no wall-clock sleep. The focused suite covers:

- next-intent persistence and service reconstruction after restart;
- completed occurrence cursor and duplicate suppression before the next due time;
- one coalesced overdue occurrence and no catch-up storm;
- durable pause and explicit resume;
- durable idempotent cancel;
- deadline termination, including restart after deadline;
- condition termination;
- stable per-occurrence identity across restart;
- conflicting recurrence definition failure;
- timezone/interval validation before persistence.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PRODUCTION_RELEASE_READY=false`
