from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


def test_cancel_committed_after_authority_read_blocks_handler(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "Ніка cancel dispatch TOCTOU" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "контрольована відкладена дія"},
    )
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)

    jobs = ScheduledJobStore(store)
    jobs.upsert(
        ScheduledJob(
            job_id="job-cancel-dispatch-race",
            action_id="batch.resume",
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
            payload={"task_id": record.task_id, "next_batch": 2},
            misfire_grace_seconds=3600,
        )
    )

    authority_read = threading.Event()
    release_dispatch = threading.Event()
    original_task_state = jobs.task_state

    def interleaved_task_state(task_id: str) -> TaskState | None:
        state = original_task_state(task_id)
        authority_read.set()
        assert release_dispatch.wait(timeout=5), "dispatch interleave was not released"
        return state

    monkeypatch.setattr(jobs, "task_state", interleaved_task_state)

    calls: list[str] = []
    errors: list[BaseException] = []

    def resolve(action_id: str):
        def handler(_payload: dict[str, object]) -> None:
            calls.append(action_id)

        return handler

    adapter = APSchedulerAdapter(jobs, resolve)

    def dispatch() -> None:
        try:
            adapter._dispatch("job-cancel-dispatch-race")
        except BaseException as exc:  # noqa: BLE001 - preserve thread failure for assertion
            errors.append(exc)

    thread = threading.Thread(target=dispatch, daemon=True)
    thread.start()
    assert authority_read.wait(timeout=5), "dispatch did not reach durable authority read"

    queue.transition(record.task_id, TaskState.CANCELLED)
    assert queue.get(record.task_id).state is TaskState.CANCELLED

    release_dispatch.set()
    thread.join(timeout=5)

    assert not thread.is_alive(), "dispatch thread did not finish"
    assert errors == []
    assert queue.get(record.task_id).state is TaskState.CANCELLED
    assert calls == []
