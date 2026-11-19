# Ordinary packaged task scheduling

Status: implementation candidate, not integrated or whole Section 7 DONE.

## Canonical reuse

The host reuses the single-host composition and one-task admission semantics of
existing #883 (`42b3e427aa5ff2476df8e7b5c461d7688cbc8085`) and #860
(`ceada6f28e3e95f014a673b69882518af1b6c20c`). Their four additive files were
also converged in historical terminal #1501. Those hosts depend on Living Agent
owner-presence/guarded background-effect contracts that are absent from the current
ordinary desktop composition; importing that unrelated stack is not required.

`PackagedTaskScheduler` is a thin ordinary-task composition of the current
`ScheduledJobStore`, `APSchedulerAdapter` and `DurableRecurrenceService`. The
qualified #994 lifecycle CAS, #884 transactional dispatch, #1107 typed snapshots,
UTC/time/deadline validation and COALESCE_ONE cursor remain their authorities.
No schema, persistence engine, scheduler, runtime, effect ledger, polling loop,
dependency or parallel task state machine is introduced.

## One delayed start, bounded admission retries

The desktop backend first prepares one ordinary task in READY without starting it.
`create(task_id, start_at, retry_interval_seconds=60, deadline_at=None)` binds one
canonical UUID task to one immutable recurrence definition. It does not create
another task or accept arbitrary action IDs, payloads or client callbacks.

The first due occurrence asks the trusted backend to admit that existing task.
The interval is an admission retry interval, not a request to rerun completed
business work. Repeating monitoring/business effects keep their existing research
or Living Agent composition and deduplication contracts.

The trusted `start_task(task_id) -> bool` port runs on the APScheduler worker.
It may submit to the incumbent desktop event loop, but waits only for bounded
durable admission proof, not completion of a potentially long-running runtime:

- exact True means a durable RUNNING event, terminal state, or prior-start proof;
- exact False means a proven pre-effect resource wait with no prior start;
- unknown/failed admission raises and leaves the exact occurrence unadvanced.

The host independently checks RUNNING history/terminal task truth before accepting
True. An early asynchronous acknowledgement cannot silently consume the intent.
Any already-started task stops the timer without fresh runtime execution; durable
runtime recovery and uncertain external-effect reconciliation remain coordinator
authority. False after an actual start is rejected as contradictory evidence.

Resource wait advances to one future admission slot using the canonical recurrence
policy. Missing many slots does not create a catch-up storm. The existing task is
retained, so no new task IDs or semantic effects are manufactured by retries.

## Controls and lifecycle

`start()` reinstalls enabled persisted intents through the canonical APScheduler
adapter. `shutdown(wait=True)` delegates shutdown without changing durable intent.
`get`, `pause`, `resume` and `cancel` take an explicit canonical task UUID and
delegate the associated recurrence lifecycle, including its transactional CAS.
The host is bound to the actual ordinary workspace and agent, not untrusted UI
workspace/action strings. Unknown scheduler/target actions are rejected before
calling the trusted runtime port.

An APScheduler DATE callback is consumed even when admission raises. Its exact
unadvanced durable occurrence correctly remains ACTIVE, so explicit resume also
reloads and activates the current persisted intent when already ACTIVE. It does
not upsert an earlier snapshot. Concurrent pause/cancel or a recreated definition
remain authoritative through the existing persisted-only activation and final
canonical readback. A late RUNNING event stops the timer without another starter
call; an exception alone never advances the occurrence cursor.

Cancel means **cancel the scheduled start/admission attempts**. It does not claim
to stop or pause an already-running runtime. The ordinary Stop action retains its
existing `TaskRuntimeCoordinator` cancellation boundary. Root desktop composition
may separately cancel a never-started prepared task after disabling its timer;
that ordering has no automatic-execution crash gap.

Canonical terminal/missing-task suppression is retained. `snapshot_page(limit=50,
after_job_id=None)` queries actual scheduled-job IDs with SQL keyset pagination
before strict payload hydration. It returns `items`, `next_after_job_id` and
`has_more`. No total result cap or unbounded materialization is introduced.
The cursor is a validated canonical job ID, a view selector rather than execution
authority. Foreign namespaces can yield an empty candidate page with a next cursor;
the UI must still expose Next. Concurrent additions appear when the operator returns
to First; pages do not claim an immutable export snapshot.

Each item contains schedule IDs, lifecycle and ISO timestamps, without commands,
callbacks or private errors. Compatibility `snapshot(limit=50)` returns first-page
items. It never consults the 50 most recent ordinary tasks: newer unrelated work
cannot hide an older schedule, and additional schedule pages remain navigable.

## Verification boundary

The dedicated host tests exercise real SQLite stores with Unicode/space paths,
actual APScheduler construction/restart and one real worker execution. They cover
delayed admission, resource retry/no storm, pause/reopen/resume, cancellation,
deadline, completed/started no-replay, terminal/missing task suppression, unknown
actions, foreign workspace/agent, invalid UUIDs, forged invocation, early/malformed
acknowledgement, exact unadvanced failure cursor and concurrent timer controls.

The caller still must wire the backend's trusted prepare/start methods, host
startup/shutdown, existing semantic action registry and Ukrainian accessible UI.
Source tests alone do not prove that packaged user journey or a human NVDA run.
Final exact-head Core/M12/Windows/package gates and independent review are required
after that composition. No human, physical performance or whole-product evidence
is awarded by this document.

`HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false`.
