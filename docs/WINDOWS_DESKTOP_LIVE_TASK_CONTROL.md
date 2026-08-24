# Windows desktop live task control

## Scope

This slice repairs the packaged Windows command surface so creating a real task does not hold the
pywebview bridge thread until runtime completion. It deliberately reuses the canonical task/runtime
stack instead of adding a second executor.

The user-visible control path remains:

`WebView2 action -> UIActionBridge -> DesktopBackend -> TaskRuntimeCoordinator -> AgentRuntimePort`

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE** `TaskQueue`, `TaskRuntimeCoordinator`, `RuntimeSessionStore`, the runtime cancellation
  idempotency ledger, `AgentRuntimePort`, `ReferenceRuntime`, and existing bridge/state rendering.
- **ADAPT** the synchronous pywebview facade to the async runtime through one private background
  asyncio loop. Start, durable resume, and cancellation calls for the desktop runtime stay on this
  same loop.
- **CUSTOM (thin)** only packaged-UI scheduling/active-thread bookkeeping and truthful control
  semantics. No new runtime framework, task database, scheduler, permission system, or dependency.

## Correctness boundaries

### Create Task

`create_task()` persists `CREATED -> READY`, schedules the canonical coordinator, and returns the
bridge action result immediately. Runtime completion remains authoritative in `TaskQueue` and audit
state; the UI action no longer claims that the task itself completed merely because dispatch was
accepted.

The desktop runtime thread identity is deterministically derived from the durable task ID. This is
local routing identity only; durable runtime restart authority still comes from the canonical
runtime session/checkpoint services.

### Stop

For a live task, DesktopBackend routes cancellation through
`TaskRuntimeCoordinator.cancel()`. This preserves the existing durable PENDING/UNCERTAIN/completed
cancellation ledger and the cancellation-wins finalization rule. A non-durable runtime may still be
cancelled while the packaged process owns the exact live task/thread identity; after process loss,
that in-memory identity is not invented on restart.

READY/PAUSED/BLOCKED tasks that have not entered active runtime execution may still be cancelled
locally through normal task-state transitions.

### Pause and resume

The current `AgentRuntimePort` exposes `run`, `resume`, and `cancel`; it does **not** expose a
user-triggered live pause primitive. Therefore DesktopBackend no longer changes a genuinely RUNNING
task to PAUSED while its runtime continues executing in the background.

- READY work may be paused before the runtime starts.
- A pre-start PAUSED task may resume from durable task-event evidence proving it never entered
  RUNNING.
- A runtime-produced PAUSED state may resume only from a canonical persisted runtime session and a
  runtime that declares `DURABLE_RESUME`.
- PAUSED work that previously entered RUNNING but has no durable runtime session fails closed rather
  than restarting from the beginning and risking duplicate side effects.
- User-triggered pause of an already RUNNING runtime remains an explicit runtime-contract gap; it
  must be added by the runtime owner rather than simulated in the UI facade.

## Acceptance evidence

Focused regressions use a cooperative long-running fake runtime to prove:

- Create Task returns while runtime work is still active;
- real task state reaches RUNNING and later COMPLETED;
- active pause fails closed without falsifying PAUSED state;
- Stop cancels a live non-durable runtime through `TaskRuntimeCoordinator`;
- pre-start pause/resume is safe;
- resume after prior RUNNING without a durable runtime session is rejected;
- a foreign/unidentified RUNNING task cannot be locally cancelled by guesswork.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.