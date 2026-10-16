from __future__ import annotations

import logging
from concurrent.futures import Future
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows


class _Host:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _backend(tmp_path: Path) -> DesktopBackend:
    store = SQLiteStore(tmp_path / "Дані Nika" / "ніка.db")
    store.initialize()
    return DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_completed_interrupt_releases_host_and_preserves_exception_identity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    interrupt_type: type[BaseException],
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host
    interruption = interrupt_type("PRIVATE_RUNTIME_INTERRUPT")
    failed: Future[object] = Future()
    failed.set_exception(interruption)
    settled: Future[object] = Future()
    settled.set_result(None)
    backend._active_futures["failed"] = failed
    backend._cancel_futures["settled"] = settled

    with caplog.at_level(logging.WARNING), pytest.raises(interrupt_type) as caught:
        backend.close()

    assert caught.value is interruption
    assert host.closed
    assert backend._runtime_loop is None
    assert backend._active_futures == {}
    assert backend._cancel_futures == {}
    assert f"exception_type={interrupt_type.__name__}" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_second_completed_interrupt_cannot_replace_first(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host
    first = KeyboardInterrupt("PRIVATE_FIRST_INTERRUPT")
    second = SystemExit("PRIVATE_SECOND_INTERRUPT")
    first_future: Future[object] = Future()
    first_future.set_exception(first)
    second_future: Future[object] = Future()
    second_future.set_exception(second)
    backend._active_futures["first"] = first_future
    backend._cancel_futures["second"] = second_future

    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt) as caught:
        backend.close()

    assert caught.value is first
    assert host.closed
    assert "exception_type=KeyboardInterrupt" in caplog.text
    assert "exception_type=SystemExit" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_completed_task_timeout_is_not_mistaken_for_active_task(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host
    failed: Future[object] = Future()
    failed.set_exception(TimeoutError("PRIVATE_PROVIDER_TIMEOUT"))
    backend._packaged_futures.add(failed)

    with caplog.at_level(logging.WARNING):
        backend.close()

    assert host.closed
    assert backend._runtime_loop is None
    assert backend._packaged_futures == set()
    assert "exception_type=TimeoutError" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_pending_task_prevents_unsafe_host_shutdown(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host

    class Pending:
        def result(self, *, timeout: float) -> None:
            assert timeout == 2
            raise TimeoutError("PRIVATE_STILL_ACTIVE")

        def done(self) -> bool:
            return False

    pending = Pending()
    backend._packaged_futures.add(pending)
    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()

    assert not host.closed
    assert backend._runtime_loop is host
    assert pending in backend._packaged_futures


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_recovery_callback_contains_completed_interrupt(
    tmp_path: Path,
    interrupt_type: type[BaseException],
) -> None:
    backend = _backend(tmp_path)
    failed: Future[object] = Future()
    failed.set_exception(interrupt_type("PRIVATE_RECOVERY_PATH"))

    backend._startup_recovery_done(object(), failed)

    snapshot = backend.startup_recovery_snapshot()
    assert snapshot["status"] == "attention"
    assert snapshot["resume_failed_count"] == 1
    backend.close()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_packaged_session_releases_all_resources_after_host_future_interrupt(
    tmp_path: Path,
    interrupt_type: type[BaseException],
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host
    interrupted = interrupt_type("PRIVATE_HOST_INTERRUPT")
    future: Future[object] = Future()
    future.set_exception(interrupted)
    backend._packaged_futures.add(future)
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=backend,
        voice=Resource("voice"),
        voice_model_setup=Resource("voice_model_setup"),
        speech=Resource("speech"),
    )
    with pytest.raises(interrupt_type) as caught:
        session.close()

    assert caught.value is interrupted
    assert closed == ["speech", "voice_model_setup", "voice"]
    assert host.closed
    assert backend._runtime_loop is None
    session.close()
    assert closed == ["speech", "voice_model_setup", "voice"]


def test_packaged_session_preserves_first_error_over_host_interrupt(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host
    interrupted: Future[object] = Future()
    interrupted.set_exception(SystemExit("PRIVATE_HOST_INTERRUPT"))
    backend._packaged_futures.add(interrupted)
    closed: list[str] = []
    speech_error = OSError("PRIVATE_SPEECH_CLOSE")

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)
            if self.name == "speech":
                raise speech_error

    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=backend,
        voice=Resource("voice"),
        voice_model_setup=Resource("voice_model_setup"),
        speech=Resource("speech"),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(OSError) as caught:
        session.close()

    assert caught.value is speech_error
    assert closed == ["speech", "voice_model_setup", "voice"]
    assert host.closed
    assert "component=backend exception_type=SystemExit" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_packaged_session_retries_real_backend_after_pending_work_settles(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    host = _Host()
    backend._runtime_loop = host

    class Pending:
        settled = False

        def result(self, *, timeout: float) -> None:
            assert timeout == 2
            if not self.settled:
                raise TimeoutError("PRIVATE_WORK_STILL_ACTIVE")

        def done(self) -> bool:
            return self.settled

    pending = Pending()
    backend._packaged_futures.add(pending)
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                closed.append(self.name)

    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=backend,
        voice=Resource("voice"),
        voice_model_setup=Resource("voice_model_setup"),
        speech=Resource("speech"),
    )
    with pytest.raises(RuntimeError, match="tasks are active"):
        session.close()
    assert not session._closed
    assert not host.closed
    assert pending in backend._packaged_futures

    pending.settled = True
    session.close()
    assert session._closed
    assert host.closed
    assert backend._runtime_loop is None
    assert backend._packaged_futures == set()
    assert closed == ["speech", "voice_model_setup", "voice"]
