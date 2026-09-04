# V0.1 monitor-until-condition lifecycle

Status: Worker 42 candidate for `V01-B05,V01-B03`.

## Scope

This slice adds only the deterministic terminal lifecycle for a recurring monitor:

`recurring SchedulerPort job -> check gate -> condition observation -> condition OR deadline -> terminal status`

It does not fetch sources, normalize observations, detect content changes, or implement a workflow
engine. Those remain separate monitoring/change-detection responsibilities.

## Reuse decision

- **REUSE** `ScheduledJob`, `ScheduledJobStore`, `SchedulerPort`, and APScheduler-backed persisted
  job mapping.
- **ADAPT** the recurring job payload as the single authoritative durable monitor lifecycle record.
- **CUSTOM (thin)** deadline/condition precedence, terminalization, recovery guard, and deterministic
  status text.

No schema migration, ML dependency, or new orchestration framework is added. Revisioned jobs use a
reserved payload field so existing databases remain compatible.

## Canonical state

The recurring monitor job owns exactly one authoritative `deadline_at` in
`_nika_monitor_until`. The derived `::deadline` DATE job is only a wake-up hint; its handler always
re-reads and validates the canonical monitor record. The recurring trigger is not given an
`end_date`, so a second deadline authority cannot drift from the monitor state.

The same monitor metadata owns one `condition_state` (`pending`, `not_met`, or `matched`) and one
optional terminal `stop_reason` (`condition_met` or `deadline_reached`).

## Deterministic terminal rules

1. A condition observed strictly before the deadline wins and disables the recurring job.
2. An observation at or after the deadline resolves as `deadline_reached`, even when the condition
   is simultaneously matched.
3. Deadline terminalization persists `enabled=false` before the derived deadline guard is removed.
   A racing APScheduler dispatch therefore re-reads a disabled durable job and fails closed.
4. Every monitor transition advances `_nika_scheduler_revision` with a full-state,
   `BEGIN IMMEDIATE` compare-and-swap. Scheduler synchronization re-reads that durable revision,
   so a stale writer cannot restore an older terminal record or runtime job.
5. Concurrent condition/deadline events are merged monotonically. A match at the exact deadline
   always converges to `deadline_reached + matched`, including after either commit order, restart,
   or replay.
6. The deadline guard uses unlimited misfire grace so restart after downtime can still reconcile the
   deadline. Every monitor tick must also call `before_check()` before external work; this blocks a
   late recurring wake-up even if the deadline guard has not dispatched first.
7. `register()` and `resume()` never reactivate a terminal monitor. Reuse the existing schedule ID
   only for the same canonical monitor; use a new ID for a new monitoring lifecycle.
8. Startup/recovery should call `reconcile()` for an active monitor. It repairs the derived deadline
   guard or terminalizes an already-expired monitor.

## Integration contract

The monitoring-loop owner should wire:

- the recurring monitor job through `register()`;
- `before_check(schedule_id)` before source/network work;
- `record_condition(...)` after the deterministic condition result;
- `research.monitor_until.deadline` to `deadline_action_handler`;
- `reconcile(schedule_id)` when restoring a durable monitor.

The final text projection from `render_status_text()` states whether monitoring stopped because the
condition matched or because the deadline was reached.

This automated slice does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
