from __future__ import annotations

import time
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend


def _backend(
    tmp_path: Path,
    *,
    admit_created_task=None,
    admit_resumed_task=None,
) -> tuple[DesktopBackend, TaskQueue, AuditLog]:
    store = SQLiteStore(tmp_path / "task-admission.sqlite3")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    return (
        DesktopBackend(
            queue=queue,
            agents=AgentRegistry(store),
            workspaces=WorkspaceRegistry(store),
            audit=audit,
            admit_created_task=admit_created_task,
            admit_resumed_task=admit_resumed_task,
        ),
        queue,
        audit,
    )


def _wait(queue: TaskQueue, task_id: str, state: TaskState) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if queue.get(task_id).state is state:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state is state


def test_rejected_admission_cancels_created_task_before_runtime_dispatch(tmp_path: Path) -> None:
    seen: list[TaskRecord] = []

    def reject(record: TaskRecord) -> None:
        seen.append(record)
        raise ValueError("Зовнішній API не дозволено.")

    backend, queue, audit = _backend(tmp_path, admit_created_task=reject)

    with pytest.raises(ValueError, match="не дозволено"):
        backend.create_task({"command": "Use selected model"})

    tasks = queue.list_recent()
    assert len(tasks) == 1
    assert len(seen) == 1
    assert seen[0].task_id == tasks[0].task_id
    assert seen[0].state is TaskState.CREATED
    assert tasks[0].state is TaskState.CANCELLED
    assert backend._runtime_loop is None
    events = audit.list_for(entity_type="task", entity_id=tasks[0].task_id)
    assert [event.event_type for event in events] == ["desktop.task_admission_rejected"]
    backend.close()


def test_admission_cannot_silently_change_state_before_ready(tmp_path: Path) -> None:
    queue_ref: list[TaskQueue] = []

    def mutate(record: TaskRecord) -> None:
        queue_ref[0].transition(record.task_id, TaskState.CANCELLED)

    backend, queue, audit = _backend(tmp_path, admit_created_task=mutate)
    queue_ref.append(queue)

    with pytest.raises(RuntimeError, match="admission changed task state"):
        backend.create_task({"command": "Do not dispatch changed state"})

    task = queue.list_recent()[0]
    assert task.state is TaskState.CANCELLED
    assert backend._runtime_loop is None
    assert [event.event_type for event in audit.list_for(
        entity_type="task",
        entity_id=task.task_id,
    )] == ["desktop.task_admission_rejected"]
    backend.close()


def test_successful_admission_runs_existing_desktop_path(tmp_path: Path) -> None:
    seen: list[TaskRecord] = []
    backend, queue, _audit = _backend(
        tmp_path,
        admit_created_task=lambda record: seen.append(record),
    )

    result = backend.create_task({"command": "Continue normal execution"})

    assert result.status == "accepted"
    task = queue.list_recent()[0]
    assert seen and seen[0].task_id == task.task_id
    assert seen[0].state is TaskState.CREATED
    _wait(queue, task.task_id, TaskState.COMPLETED)
    backend.close()


def test_admission_audit_does_not_capture_callback_exception_text(tmp_path: Path) -> None:
    def fail(_record: TaskRecord) -> None:
        raise ValueError("PRIVATE_CONFIRMATION_CANARY")

    backend, queue, audit = _backend(tmp_path, admit_created_task=fail)

    with pytest.raises(ValueError, match="PRIVATE_CONFIRMATION_CANARY"):
        backend.create_task({"command": "Reject safely"})

    task = queue.list_recent()[0]
    events = audit.list_for(entity_type="task", entity_id=task.task_id)
    assert "PRIVATE_CONFIRMATION_CANARY" not in repr(events)
    backend.close()


def _paused_never_started(queue: TaskQueue, command: str) -> TaskRecord:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.PAUSED)
    return queue.get(record.task_id)


def test_rejected_resume_admission_leaves_paused_task_unsubmitted(tmp_path: Path) -> None:
    seen: list[TaskRecord] = []

    def reject(record: TaskRecord) -> None:
        seen.append(record)
        raise ValueError("Потрібне нове підтвердження.")

    backend, queue, _audit = _backend(tmp_path, admit_resumed_task=reject)
    paused = _paused_never_started(queue, "Resume only after consent")

    with pytest.raises(ValueError, match="нове підтвердження"):
        backend.resume_task({})

    assert len(seen) == 1
    assert seen[0] == paused
    assert queue.get(paused.task_id).state is TaskState.PAUSED
    assert backend._runtime_loop is None
    assert backend._active_futures == {}
    backend.close()


def test_successful_resume_admission_preserves_existing_resume_path(tmp_path: Path) -> None:
    seen: list[TaskRecord] = []
    backend, queue, _audit = _backend(
        tmp_path,
        admit_resumed_task=lambda record: seen.append(record),
    )
    paused = _paused_never_started(queue, "Resume normal path")

    result = backend.resume_task({})

    assert result.status == "accepted"
    assert len(seen) == 1
    assert seen[0] == paused
    _wait(queue, paused.task_id, TaskState.COMPLETED)
    backend.close()
