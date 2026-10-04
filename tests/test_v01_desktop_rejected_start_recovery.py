from __future__ import annotations

import inspect
from concurrent.futures import Future
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend


class _RejectingHost:
    def __init__(self) -> None:
        self.failure = OSError("PRIVATE_HOST_CREDENTIAL")
        self.attempted: list[object] = []
        self.closed = False

    def submit(self, coroutine: object) -> Future[object]:
        self.attempted.append(coroutine)
        raise self.failure

    def close(self) -> None:
        self.closed = True


def _backend(tmp_path: Path, host: _RejectingHost) -> DesktopBackend:
    store = SQLiteStore(tmp_path / "Дані Nika" / "ніка.db")
    store.initialize()
    backend = DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    backend._runtime_loop = host
    return backend


def _assert_failed_submission(
    backend: DesktopBackend, host: _RejectingHost, task_id: str
) -> None:
    assert backend._queue.get(task_id).state is TaskState.PAUSED
    assert backend._active_threads == {}
    assert backend._active_futures == {}
    assert len(host.attempted) == 1
    assert inspect.getcoroutinestate(host.attempted[0]) == inspect.CORO_CLOSED
    events = backend._audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events] == [
        "desktop.runtime_submission_failed"
    ]
    assert "PRIVATE_HOST_CREDENTIAL" not in str(events)


def test_rejected_new_task_is_durable_paused_not_orphan_ready(tmp_path: Path) -> None:
    host = _RejectingHost()
    backend = _backend(tmp_path, host)

    with pytest.raises(OSError) as failure:
        backend.create_task({"command": "Do not lose this task"})
    assert failure.value is host.failure

    task = backend._queue.list_recent(limit=10)
    assert len(task) == 1
    assert task[0].payload["command"] == "Do not lose this task"
    _assert_failed_submission(backend, host, task[0].task_id)
    backend.close()
    assert host.closed


def test_rejected_resume_keeps_manual_recovery_paused(tmp_path: Path) -> None:
    host = _RejectingHost()
    backend = _backend(tmp_path, host)
    task = backend._queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Resume when a host exists"},
    )
    backend._queue.transition(task.task_id, TaskState.READY)
    backend._queue.transition(task.task_id, TaskState.PAUSED)

    with pytest.raises(OSError) as failure:
        backend.resume_task({})
    assert failure.value is host.failure
    _assert_failed_submission(backend, host, task.task_id)
    backend.close()
    assert host.closed


def test_failed_reconciliation_does_not_hide_original_host_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _RejectingHost()
    backend = _backend(tmp_path, host)
    transition = backend._queue.transition

    def fail_reconciliation(task_id: str, state: TaskState) -> TaskState:
        if state is TaskState.PAUSED:
            raise OSError("PRIVATE_DATABASE_FAILURE")
        return transition(task_id, state)

    monkeypatch.setattr(backend._queue, "transition", fail_reconciliation)
    with pytest.raises(OSError) as failure:
        backend.create_task({"command": "Preserve primary failure"})
    assert failure.value is host.failure
    assert len(host.attempted) == 1
    assert inspect.getcoroutinestate(host.attempted[0]) == inspect.CORO_CLOSED
    backend.close()

def test_early_async_host_failure_preserves_ready_task_for_manual_resume(
    tmp_path: Path,
) -> None:
    host = _RejectingHost()
    backend = _backend(tmp_path, host)
    task = backend._queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Asynchronous failure before runtime start"},
    )
    backend._queue.transition(task.task_id, TaskState.READY)
    failed: Future[object] = Future()
    failed.set_exception(RuntimeError("PRIVATE_ASYNC_FAILURE"))
    backend._active_futures[task.task_id] = failed
    backend._active_threads[task.task_id] = "failed-before-running"

    backend._runtime_done(task.task_id, failed)

    assert backend._queue.get(task.task_id).state is TaskState.PAUSED
    assert task.task_id not in backend._active_futures
    assert task.task_id not in backend._active_threads
    events = backend._audit.list_for(entity_type="task", entity_id=task.task_id)
    assert [event.event_type for event in events] == ["desktop.runtime_host_failed"]
    assert "PRIVATE_ASYNC_FAILURE" not in str(events)
    backend.close()


def test_stale_async_failure_does_not_pause_new_ready_runtime(tmp_path: Path) -> None:
    host = _RejectingHost()
    backend = _backend(tmp_path, host)
    task = backend._queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Replacement remains active"},
    )
    backend._queue.transition(task.task_id, TaskState.READY)
    stale: Future[object] = Future()
    stale.set_exception(RuntimeError("OLD_FAILURE"))
    replacement: Future[object] = Future()
    backend._active_futures[task.task_id] = replacement
    backend._active_threads[task.task_id] = "replacement-thread"

    backend._runtime_done(task.task_id, stale)

    assert backend._queue.get(task.task_id).state is TaskState.READY
    assert backend._active_futures[task.task_id] is replacement
    assert backend._active_threads[task.task_id] == "replacement-thread"
    replacement.set_result(None)
    backend.close()
