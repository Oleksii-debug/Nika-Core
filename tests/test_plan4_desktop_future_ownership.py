from __future__ import annotations

import inspect
import time
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


def _build(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue


async def _noop() -> None:
    return None


def test_duplicate_submission_closes_unscheduled_coroutine(tmp_path: Path) -> None:
    backend, _queue = _build(tmp_path)
    existing: Future[object] = Future()
    backend._active_futures["task-id"] = existing
    coroutine = _noop()
    try:
        with pytest.raises(ValueError, match="активне runtime-виконання"):
            backend._submit_runtime("task-id", "new-thread", coroutine)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert backend._active_futures["task-id"] is existing
        assert "task-id" not in backend._active_threads
    finally:
        backend._active_futures.clear()
        backend.close()


def test_submit_failure_does_not_record_phantom_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _queue = _build(tmp_path)

    def broken_host() -> None:
        raise RuntimeError("host unavailable")

    monkeypatch.setattr(backend, "_host", broken_host)
    coroutine = _noop()
    try:
        with pytest.raises(RuntimeError, match="host unavailable"):
            backend._submit_runtime("task-id", "thread-id", coroutine)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert backend._active_futures == {}
        assert backend._active_threads == {}
    finally:
        backend.close()


def test_late_failed_runtime_callback_does_not_erase_successor_or_fail_task(
    tmp_path: Path,
) -> None:
    backend, queue = _build(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "safe"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    old: Future[object] = Future()
    old.set_exception(RuntimeError("old runtime failed"))
    successor: Future[object] = Future()
    backend._active_threads[task.task_id] = "new-thread"
    backend._active_futures[task.task_id] = successor
    try:
        backend._runtime_done(task.task_id, old)
        assert backend._active_threads[task.task_id] == "new-thread"
        assert backend._active_futures[task.task_id] is successor
        assert queue.get(task.task_id).state == TaskState.RUNNING
    finally:
        backend._active_threads.clear()
        backend._active_futures.clear()
        backend.close()


def test_late_cancel_callback_preserves_newer_cancellation(tmp_path: Path) -> None:
    backend, _queue = _build(tmp_path)
    old: Future[bool] = Future()
    old.set_result(False)
    successor: Future[bool] = Future()
    backend._cancel_futures["task-id"] = successor
    try:
        backend._cancel_done("task-id", old)
        assert backend._cancel_futures["task-id"] is successor
        assert not successor.done()
    finally:
        backend._cancel_futures.clear()
        backend.close()


def test_current_cancel_callback_still_releases_ownership(tmp_path: Path) -> None:
    backend, _queue = _build(tmp_path)
    current: Future[bool] = Future()
    backend._cancel_futures["task-id"] = current
    current.set_result(True)
    backend._cancel_done("task-id", current)
    assert "task-id" not in backend._cancel_futures
    backend.close()


def test_completed_submission_releases_ownership(tmp_path: Path) -> None:
    backend, _queue = _build(tmp_path)
    try:
        backend._submit_runtime("task-id", "thread-id", _noop())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with backend._active_lock:
                if "task-id" not in backend._active_futures:
                    break
            time.sleep(0.01)
        with backend._active_lock:
            assert "task-id" not in backend._active_futures
            assert "task-id" not in backend._active_threads
    finally:
        backend.close()


def test_unqualified_pause_fails_closed_with_targets_outside_latest_50(tmp_path: Path) -> None:
    backend, queue = _build(tmp_path)
    first = queue.create(
        workspace_id="default", agent_id="nika.default", payload={"command": "first"}
    )
    second = queue.create(
        workspace_id="default", agent_id="nika.default", payload={"command": "second"}
    )
    queue.transition(first.task_id, TaskState.READY)
    queue.transition(second.task_id, TaskState.READY)
    # Both live targets are obscured by newer completed tasks in the UI snapshot.
    for index in range(55):
        terminal = queue.create(
            workspace_id="default",
            agent_id="nika.default",
            payload={"command": f"done-{index}"},
        )
        queue.transition(terminal.task_id, TaskState.READY)
        queue.transition(terminal.task_id, TaskState.RUNNING)
        queue.transition(terminal.task_id, TaskState.COMPLETED)
    try:
        with pytest.raises(ValueError, match="кілька завдань"):
            backend.pause_task({})
        assert queue.get(first.task_id).state == TaskState.READY
        assert queue.get(second.task_id).state == TaskState.READY
    finally:
        backend.close()


def test_unqualified_actions_never_select_other_agent_or_workspace(tmp_path: Path) -> None:
    backend, queue = _build(tmp_path)
    foreign = queue.create(
        workspace_id="another-workspace", agent_id="another-agent",
        payload={"command": "do not control"},
    )
    queue.transition(foreign.task_id, TaskState.READY)
    try:
        with pytest.raises(ValueError, match="Немає активного завдання"):
            backend.pause_task({})
        with pytest.raises(ValueError, match="Немає активного завдання агента"):
            backend.stop_agent({})
        assert queue.get(foreign.task_id).state == TaskState.READY
    finally:
        backend.close()


def test_unqualified_resume_detects_old_paused_target_behind_recent_history(tmp_path: Path) -> None:
    backend, queue = _build(tmp_path)
    paused = queue.create(
        workspace_id="default", agent_id="nika.default", payload={"command": "resume me"}
    )
    queue.transition(paused.task_id, TaskState.READY)
    queue.transition(paused.task_id, TaskState.PAUSED)
    # Newer terminal rows must not hide the only resumable task.
    for index in range(55):
        terminal = queue.create(
            workspace_id="default", agent_id="nika.default",
            payload={"command": f"done-{index}"},
        )
        queue.transition(terminal.task_id, TaskState.READY)
        queue.transition(terminal.task_id, TaskState.RUNNING)
        queue.transition(terminal.task_id, TaskState.COMPLETED)
    try:
        assert backend.resume_task({}).status == "accepted"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if queue.get(paused.task_id).state == TaskState.COMPLETED:
                break
            time.sleep(0.01)
        assert queue.get(paused.task_id).state == TaskState.COMPLETED
    finally:
        backend.close()
