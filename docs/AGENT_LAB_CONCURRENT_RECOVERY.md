# Agent Lab concurrent recovery boundary

Date: 2026-08-19.
Lane: AUTO03 generic Agent Lab / multi-agent lifecycle.

## Defect

`LangGraphAgentRuntime` rejects a duplicate active task inside one runtime instance, but that rejection is represented as `RuntimeOutcome.FAILED` with `DUPLICATE_ACTIVE`. Before this repair, two concurrent calls to `MultiAgentSupervisor.recover_team()` on the same supervisor could therefore race on one persisted `RUNNING` child: the first call could be legitimately resuming it while the second call received the duplicate-active failure and caused M7 to persist a false child failure.

## Repair

`MultiAgentSupervisor` now serializes recovery per `team_id` within one supervisor instance. While one recovery call owns that team, another concurrent call returns no work and does not call `runtime.resume`. After the active recovery finishes, the guard is released in `finally`; a later ordinary recovery call re-reads durable member state and therefore sees terminal work as already completed.

This is deliberately a **single-supervisor-instance guarantee**, not a distributed lease claim. Nika does not pretend that an in-memory guard coordinates independent processes. The normal desktop recovery model remains one active supervisor process reopening the durable SQLite store after the previous process has exited or crashed. A future true multi-process worker topology would require a separate persisted lease/ownership protocol before it could claim cross-process exactly-once recovery.

## Regression

`tests/test_agent_lab_concurrent_recovery.py` creates a persisted `RUNNING` child with a durable resume token, launches two `recover_team()` calls concurrently on the same supervisor, holds the first `resume` open long enough to expose the race, and requires:

- exactly one runtime resume request;
- one recovery caller receives the child execution while the duplicate caller receives no work;
- the durable child becomes `COMPLETED`;
- a later recovery call remains idempotent and emits no second resume.

## Evidence boundary

This guard complements, but does not replace, the crash-atomic store/result boundaries documented in `AGENT_LAB_DURABLE_TEAM_LIFECYCLE.md`. Automatic CI may prove the deterministic single-supervisor contract. It does not prove distributed exactly-once semantics, `HUMAN_TESTED`, or `NVDA_VERIFIED`.
