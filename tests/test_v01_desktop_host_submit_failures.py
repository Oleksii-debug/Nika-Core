from __future__ import annotations

import inspect
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.ui.desktop_backend as desktop_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.runtime.contracts import RuntimeCapability
from nika_core.runtime.recovery import RecoveryDisposition
from nika_core.ui.desktop_backend import DesktopBackend


class RejectingHost:
    def __init__(self) -> None:
        self.attempted: list[object] = []
        self.closed = False

    def submit(self, coroutine: object) -> Future[object]:
        self.attempted.append(coroutine)
        raise OSError("PRIVATE_SUBMISSION_FAILURE")

    def close(self) -> None:
        self.closed = True


async def _never_run() -> None:
    raise AssertionError("failed submission must not execute coroutine")


def _backend(tmp_path: Path, host: RejectingHost | None = None) -> DesktopBackend:
    store = SQLiteStore(tmp_path / "Дані Nika" / "ніка.db")
    store.initialize()
    backend = DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    if host is not None:
        backend._runtime_loop = host
    return backend


def test_duplicate_runtime_submit_closes_rejected_coroutine(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    existing: Future[object] = Future()
    backend._active_futures["task"] = existing
    backend._active_threads["task"] = "original-thread"
    coroutine = _never_run()

    with pytest.raises(ValueError, match="активне runtime-виконання"):
        backend._submit_runtime("task", "duplicate-thread", coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert backend._active_futures["task"] is existing
    assert backend._active_threads["task"] == "original-thread"
    existing.set_result(None)
    backend.close()


def test_failed_runtime_submit_rolls_back_identity_and_closes_coroutine(
    tmp_path: Path,
) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)
    coroutine = _never_run()

    with pytest.raises(OSError, match="PRIVATE_SUBMISSION_FAILURE"):
        backend._submit_runtime("task", "unsubmitted-thread", coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert "task" not in backend._active_threads
    assert "task" not in backend._active_futures
    assert host.attempted == [coroutine]
    backend.close()
    assert host.closed


def test_failed_cancel_submit_keeps_previous_tracking_and_closes_coroutine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)
    monkeypatch.setattr(
        backend._runtime, "capabilities", frozenset({RuntimeCapability.CANCELLATION})
    )

    with backend._active_lock, pytest.raises(OSError, match="PRIVATE_SUBMISSION_FAILURE"):
        backend._schedule_cancel_locked("task", "thread")

    assert backend._cancel_futures == {}
    assert len(host.attempted) == 1
    assert inspect.getcoroutinestate(host.attempted[0]) == inspect.CORO_CLOSED
    backend.close()


def test_failed_packaged_submit_does_not_track_or_leak_coroutine(tmp_path: Path) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)
    coroutine = _never_run()

    with pytest.raises(OSError, match="PRIVATE_SUBMISSION_FAILURE"):
        backend.submit_packaged_coroutine(coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert backend._packaged_futures == set()
    backend.close()


def test_failed_recovery_submit_reports_attention_not_perpetual_recovering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)

    class Recovery:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def inspect(self) -> tuple[SimpleNamespace, ...]:
            return (SimpleNamespace(disposition=RecoveryDisposition.AUTO_RESUME_CRASH),)

        async def resume_safe_crash_sessions(self) -> tuple[()]:
            return ()

    monkeypatch.setattr(desktop_module, "RuntimeRecoveryService", Recovery)

    with pytest.raises(OSError, match="PRIVATE_SUBMISSION_FAILURE"):
        backend.start_startup_recovery(startup_wait_seconds=0)

    state = backend.startup_recovery_snapshot()
    assert state["status"] == "attention"
    assert state["resume_failed_count"] == 1
    assert backend._startup_recovery_future is None
    assert len(host.attempted) == 1
    assert inspect.getcoroutinestate(host.attempted[0]) == inspect.CORO_CLOSED
    backend.close()


def test_shutdown_fence_rejects_new_work_before_and_after_host_stop(tmp_path: Path) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)
    entered = threading.Event()
    release = threading.Event()

    class Pending(Future[object]):
        def result(self, timeout: float | None = None) -> object:
            entered.set()
            assert release.wait(timeout=5)
            return super().result(timeout=timeout)

    pending = Pending()
    backend._packaged_futures.add(pending)
    errors: list[BaseException] = []

    def close_backend() -> None:
        try:
            backend.close()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=close_backend)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        rejected = _never_run()
        with pytest.raises(RuntimeError, match="shutting down"):
            backend.submit_packaged_coroutine(rejected)
        assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED
        assert backend._packaged_futures == {pending}
    finally:
        pending.set_result(None)
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert host.closed
    assert backend._packaged_futures == set()
    after_close = _never_run()
    with pytest.raises(RuntimeError, match="shutting down"):
        backend._submit_runtime("later-task", "later-thread", after_close)
    assert inspect.getcoroutinestate(after_close) == inspect.CORO_CLOSED
    backend.close()


def test_pending_shutdown_restores_explicit_cancel_and_close_retry(tmp_path: Path) -> None:
    host = RejectingHost()
    backend = _backend(tmp_path, host)

    class Pending(Future[object]):
        def result(self, timeout: float | None = None) -> object:
            if not self.done():
                raise TimeoutError("PRIVATE_STILL_ACTIVE")
            return super().result(timeout=timeout)

    pending = Pending()
    backend._active_futures["task"] = pending
    backend._active_threads["task"] = "current-thread"
    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()

    assert backend._accepting
    assert not backend._closing
    assert not host.closed
    assert backend._active_futures["task"] is pending
    pending.set_result(None)
    backend.close()
    assert host.closed
    assert not backend._accepting
