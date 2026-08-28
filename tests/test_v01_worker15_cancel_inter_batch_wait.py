from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind
from nika_core.ui.desktop_backend import DesktopBackend


@dataclass
class _FakeClock:
    now: datetime

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _DeterministicDueRunner:
    """Drive canonical durable DATE jobs from an injected clock, never wall-clock sleep."""

    def __init__(
        self,
        *,
        jobs: ScheduledJobStore,
        adapter: APSchedulerAdapter,
        clock: _FakeClock,
    ) -> None:
        self._jobs = jobs
        self._adapter = adapter
        self._clock = clock

    def run_due(self) -> None:
        for job in self._jobs.list_enabled():
            if job.trigger_kind is not TriggerKind.DATE:
                continue
            raw_run_date = job.trigger.get("run_date")
            if not isinstance(raw_run_date, str):
                raise TypeError("test oracle requires persisted ISO-8601 DATE run_date")
            run_date = datetime.fromisoformat(raw_run_date)
            if run_date.tzinfo is None or run_date.utcoffset() is None:
                raise ValueError("test oracle requires timezone-aware DATE run_date")
            if run_date.astimezone(UTC) <= self._clock.now.astimezone(UTC):
                self._adapter._dispatch(job.job_id)


def _backend(store: SQLiteStore) -> tuple[DesktopBackend, TaskQueue]:
    queue = TaskQueue(store)
    return (
        DesktopBackend(
            queue=queue,
            agents=AgentRegistry(store),
            workspaces=WorkspaceRegistry(store),
            audit=AuditLog(store),
        ),
        queue,
    )


def _run_browser_effect_once(
    *,
    ledger: IdempotencyLedger,
    task_id: str,
    effect_id: str,
    browser_calls: list[str],
) -> None:
    operation_key = f"{task_id}:browser:{effect_id}"
    record, created = ledger.reserve_once(
        operation_key=operation_key,
        task_id=task_id,
        operation_type="browser.effect",
        input_fingerprint=effect_id,
    )
    if record.status is IdempotencyStatus.COMPLETED:
        return
    if not created:
        raise AssertionError("unresolved browser effect cannot be replayed automatically")
    browser_calls.append(effect_id)
    ledger.complete(operation_key, {"verified": True, "effect_id": effect_id})


def test_cancel_during_inter_batch_wait_is_terminal_across_restart(tmp_path: Path) -> None:
    """V0.1 B03/B04: Cancel must outrank a persisted next-batch wake after restart."""
    db_path = tmp_path / "Ніка QA wait" / "nika.db"
    store = SQLiteStore(db_path)
    store.initialize()
    backend, queue = _backend(store)

    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "обробити контрольовані веб-цілі двома пакетами"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    browser_calls: list[str] = []
    ledger = IdempotencyLedger(store)
    _run_browser_effect_once(
        ledger=ledger,
        task_id=task.task_id,
        effect_id="batch-1-confirmed",
        browser_calls=browser_calls,
    )
    queue.transition(task.task_id, TaskState.BLOCKED)

    clock = _FakeClock(datetime(2035, 4, 5, 10, 0, tzinfo=UTC))
    wake_at = clock.now + timedelta(minutes=20)
    job_id = f"{task.task_id}:batch-2"
    ScheduledJobStore(store).upsert(
        ScheduledJob(
            job_id=job_id,
            action_id="batch.resume",
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": wake_at.isoformat()},
            payload={"task_id": task.task_id, "next_batch": 2},
            coalesce=True,
            max_instances=1,
            misfire_grace_seconds=60,
        )
    )

    first_cancel = backend.stop_agent({})
    assert first_cancel.status == "completed"
    assert queue.get(task.task_id).state is TaskState.CANCELLED

    repeated_cancel_error: str | None = None
    try:
        backend.stop_agent({})
    except ValueError as exc:
        repeated_cancel_error = str(exc)

    backend.close()

    reopened_store = SQLiteStore(db_path)
    reopened_store.initialize()
    reopened_backend, reopened_queue = _backend(reopened_store)
    report = reopened_backend.snapshot()
    report_task = next(item for item in report["tasks"] if item["task_id"] == task.task_id)

    next_batches_started: list[int] = []
    restarted_ledger = IdempotencyLedger(reopened_store)

    def resume_next_batch(payload: dict[str, object]) -> None:
        next_batches_started.append(int(payload["next_batch"]))
        # Restart may inspect/replay batch 1, but the durable effect ledger must not execute it again.
        _run_browser_effect_once(
            ledger=restarted_ledger,
            task_id=task.task_id,
            effect_id="batch-1-confirmed",
            browser_calls=browser_calls,
        )
        _run_browser_effect_once(
            ledger=restarted_ledger,
            task_id=task.task_id,
            effect_id="batch-2-new",
            browser_calls=browser_calls,
        )

    restarted_jobs = ScheduledJobStore(reopened_store)
    restarted_scheduler = APSchedulerAdapter(
        restarted_jobs,
        lambda action_id: resume_next_batch,
    )
    clock.advance(timedelta(minutes=21))
    _DeterministicDueRunner(
        jobs=restarted_jobs,
        adapter=restarted_scheduler,
        clock=clock,
    ).run_due()

    completed_batch_one = restarted_ledger.require(
        f"{task.task_id}:browser:batch-1-confirmed"
    )
    observed = {
        "next_batches_started": next_batches_started,
        "future_browser_effects": [
            effect_id for effect_id in browser_calls if effect_id == "batch-2-new"
        ],
        "batch_one_effect_count": browser_calls.count("batch-1-confirmed"),
        "terminal_state": reopened_queue.get(task.task_id).state,
        "repeated_cancel_idempotent": repeated_cancel_error is None,
        "report_state": report_task["state"],
        "report_command": report_task["command"],
        "completed_batch_one_status": completed_batch_one.status,
    }
    assert observed == {
        "next_batches_started": [],
        "future_browser_effects": [],
        "batch_one_effect_count": 1,
        "terminal_state": TaskState.CANCELLED,
        "repeated_cancel_idempotent": True,
        "report_state": TaskState.CANCELLED.value,
        "report_command": "обробити контрольовані веб-цілі двома пакетами",
        "completed_batch_one_status": IdempotencyStatus.COMPLETED,
    }
    reopened_backend.close()
