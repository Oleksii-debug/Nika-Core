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

A submitted desktop runtime identity is registered under the same local lock that Stop uses to
resolve cancellation authority. Stop therefore does not trust the earlier task snapshot returned by
unqualified target selection. While holding that lock it first checks for an in-process submitted
runtime, then for a persisted runtime session, and only then re-reads current durable task state.
This closes the READY-to-RUNNING race where stale UI state could otherwise write `CANCELLED` after
runtime execution had already started without sending the runtime a cancellation request.

READY/PAUSED/BLOCKED tasks may still be cancelled locally only when the packaged process owns no
submitted runtime identity and no persisted runtime session at the linearized Stop decision.

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

### Unqualified control authority

The packaged Pause, Resume, and Stop actions do not currently carry an explicit durable task target.
DesktopBackend therefore refuses to select an arbitrary/latest task when more than one matching
non-terminal target exists. Ambiguous unqualified control fails closed with a clear message and
causes no task-state or runtime-cancellation side effect. A future explicit task-selection UI may
extend the bridge contract without weakening this boundary.

### Teardown diagnostics

A background runtime/cancellation future can already have recorded its bounded failure through the
canonical callback before `DesktopBackend.close()` observes the failed future. Teardown no longer
silently swallows that exception. It logs only the exception **type**, never raw exception text or
payload data, then continues deterministic cleanup after all submitted work has settled.

## Acceptance evidence

Focused regressions use cooperative long-running fake runtimes to prove:

- Create Task returns while runtime work is still active and reports dispatch as `accepted`;
- real task state reaches RUNNING and later COMPLETED;
- active pause fails closed without falsifying PAUSED state;
- Stop cancels a live non-durable runtime through `TaskRuntimeCoordinator` without blocking for
  completion;
- immediate Stop after an accepted Create follows the already-submitted runtime identity instead of
  taking a local READY cancellation shortcut;
- a deterministic stale READY snapshot cannot bypass runtime cancellation after that task has become
  RUNNING;
- pre-start pause/resume is safe and asynchronous resume reports `accepted`;
- resume after prior RUNNING without a durable runtime session is rejected;
- a foreign/unidentified RUNNING task cannot be locally cancelled by guesswork;
- two live tasks make unqualified Stop fail closed with zero cancellation side effects;
- multiple candidate tasks make unqualified Pause/Resume fail closed instead of choosing by recency.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
