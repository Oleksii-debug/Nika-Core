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
from nika_core.runtime.contracts import RuntimeCapability
from nika_core.ui.desktop_backend import DesktopBackend


class _Host:
    def __init__(self) -> None:
        self.closed = False
        self.submissions = 0

    def submit(self, coroutine: object) -> Future[bool]:
        self.submissions += 1
        coroutine.close()
        future: Future[bool] = Future()
        future.set_result(True)
        return future

    def close(self) -> None:
        self.closed = True


class _Pending(Future[object]):
    def result(self, timeout: float | None = None) -> object:
        if not self.done():
            raise TimeoutError("PRIVATE_WORK_STILL_ACTIVE")
        return super().result(timeout=timeout)


async def _unsubmitted() -> None:
    raise AssertionError("work rejected during shutdown must not run")


def _backend(tmp_path: Path, host: _Host) -> DesktopBackend:
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


def test_refused_close_blocks_new_work_without_orphaning_a_task(tmp_path: Path) -> None:
    host = _Host()
    backend = _backend(tmp_path, host)
    pending = _Pending()
    backend._packaged_futures.add(pending)
    before = backend._queue.list_recent(limit=10)

    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()
    assert backend._accepting  # Explicit cancellation remains possible.
    assert backend._shutdown_pending
    with pytest.raises(RuntimeError, match="shutting down"):
        backend.create_task({"command": "Do not enqueue during shutdown"})
    assert backend._queue.list_recent(limit=10) == before

    runtime_coroutine = _unsubmitted()
    with pytest.raises(RuntimeError, match="shutting down"):
        backend._submit_runtime("late", "late-thread", runtime_coroutine)
    assert inspect.getcoroutinestate(runtime_coroutine) == inspect.CORO_CLOSED

    packaged_coroutine = _unsubmitted()
    with pytest.raises(RuntimeError, match="shutting down"):
        backend.submit_packaged_coroutine(packaged_coroutine)
    assert inspect.getcoroutinestate(packaged_coroutine) == inspect.CORO_CLOSED
    assert host.submissions == 0

    with pytest.raises(RuntimeError, match="shutting down"):
        backend.start_startup_recovery(startup_wait_seconds=0)
    assert backend.startup_recovery_snapshot()["status"] == "not_started"

    pending.set_result(None)
    backend.close()
    assert host.closed


def test_refused_close_keeps_paused_task_paused(tmp_path: Path) -> None:
    host = _Host()
    backend = _backend(tmp_path, host)
    task = backend._queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Existing paused work"},
    )
    backend._queue.transition(task.task_id, TaskState.READY)
    backend._queue.transition(task.task_id, TaskState.PAUSED)
    pending = _Pending()
    backend._packaged_futures.add(pending)

    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()
    with pytest.raises(RuntimeError, match="shutting down"):
        backend.resume_task({})
    assert backend._queue.get(task.task_id).state is TaskState.PAUSED
    assert backend._active_futures == {}

    pending.set_result(None)
    backend.close()
    assert host.closed


def test_refused_close_still_allows_explicit_cancel_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _Host()
    backend = _backend(tmp_path, host)
    monkeypatch.setattr(
        backend._runtime, "capabilities", frozenset({RuntimeCapability.CANCELLATION})
    )
    pending = _Pending()
    backend._active_futures["task"] = pending
    backend._active_threads["task"] = "existing-thread"

    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()
    with backend._active_lock:
        cancelled = backend._schedule_cancel_locked("task", "existing-thread")
    assert cancelled.result() is True
    assert host.submissions == 1
    pending.set_result(None)
    backend.close()
    assert host.closed

    with pytest.raises(RuntimeError, match="shutting down"):
        backend.stop_agent({})
    with pytest.raises(RuntimeError, match="shutting down"):
        backend.create_task({"command": "after close"})

def test_local_cancel_cannot_mutate_after_close_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _Host()
    backend = _backend(tmp_path, host)
    task = backend._queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Already queued"},
    )
    backend._queue.transition(task.task_id, TaskState.READY)

    def close_before_mutation(*, action: str) -> object:
        assert action == "зупинки"
        backend.close()
        return task

    monkeypatch.setattr(backend, "_only_controllable", close_before_mutation)
    with pytest.raises(RuntimeError, match="shutting down"):
        backend.stop_agent({})
    assert backend._queue.get(task.task_id).state is TaskState.READY
    assert host.closed
