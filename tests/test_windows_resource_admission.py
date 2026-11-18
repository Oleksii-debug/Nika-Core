from __future__ import annotations

import asyncio
import threading
import time

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.resources import ResourceManager, ResourceSnapshot
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows

_SCOPE = "desktop_runtime"
_OWNER = "local_machine"


class Observer:
    def snapshot(self):
        return ResourceSnapshot(10.0, 20.0, 2_000_000_000)


class BlockingRuntime:
    runtime_id = "resource-admission-test"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self, *, fail: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.fail = fail
        self.calls = 0

    async def run(self, _request):
        self.calls += 1
        self.started.set()
        while not self.release.is_set() and not self.cancelled.is_set():
            await asyncio.sleep(0.005)
        if self.cancelled.is_set():
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        if self.fail:
            raise OSError("fixture failure")
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, _request):
        raise AssertionError("fresh queued tasks must use run, never resume")

    async def cancel(self, **_identity):
        self.cancelled.set()
        return True


class PausingRuntime(BlockingRuntime):
    capabilities = frozenset({
        RuntimeCapability.CANCELLATION, RuntimeCapability.DURABLE_RESUME,
    })

    def __init__(self) -> None:
        super().__init__()
        self.resume_calls = 0

    def initial_resume_token(self, **_identity):
        return "initial-resource-checkpoint"

    async def run(self, _request):
        self.calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.PAUSED, resume_token="resource-checkpoint")

    async def probe_resume(self, **_identity):
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="Fixture checkpoint exists.",
            checkpoint_id="resource-checkpoint",
        )

    async def resume(self, _request):
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)


def _build(store, runtime):
    manager = ResourceManager(store, Observer())
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
        resources=manager,
    )
    return backend, queue, manager


def _wait(predicate) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def _store(tmp_path):
    store = SQLiteStore(tmp_path / "Ресурси Windows зі пробілами.db")
    store.initialize()
    return store


def test_two_backends_share_capacity_and_explicit_fresh_retry(tmp_path) -> None:
    store = _store(tmp_path)
    first_runtime = BlockingRuntime()
    second_runtime = BlockingRuntime()
    first, queue, manager = _build(store, first_runtime)
    second, _, other = _build(SQLiteStore(store.path), second_runtime)
    try:
        assert first.create_task({"command": "first"}).status == "accepted"
        assert first_runtime.started.wait(1)
        assert second.create_task({"command": "second"}).status == "accepted"
        tasks = {row.payload["command"]: row for row in queue.list_recent()}
        first_id, second_id = tasks["first"].task_id, tasks["second"].task_id
        _wait(lambda: queue.get(second_id).state == TaskState.PAUSED)
        assert not second_runtime.started.is_set()
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
        assert other.queued(scope=_SCOPE, owner_id=_OWNER) == (second_id,)
        assert second._coordinator.sessions.get(second_id) is None
        view = next(row for row in second.snapshot()["tasks"] if row["task_id"] == second_id)
        assert "Продовжити" in view["resource_message"]

        first_runtime.release.set()
        _wait(lambda: queue.get(first_id).state == TaskState.COMPLETED)
        _wait(lambda: manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0)
        assert second.resume_task({}).status == "accepted"
        assert second_runtime.started.wait(1)
        assert second_runtime.calls == 1
        view = next(row for row in second.snapshot()["tasks"] if row["task_id"] == second_id)
        assert view["resource_message"] == ""
        second_runtime.release.set()
        _wait(lambda: queue.get(second_id).state == TaskState.COMPLETED)
        _wait(lambda: manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0)
    finally:
        first_runtime.release.set()
        second_runtime.release.set()
        first.close()
        second.close()


def test_runtime_failure_releases_real_backend_capacity(tmp_path) -> None:
    runtime = BlockingRuntime(fail=True)
    backend, queue, manager = _build(_store(tmp_path), runtime)
    try:
        backend.create_task({"command": "failing runtime"})
        assert runtime.started.wait(1)
        task_id = queue.list_recent()[0].task_id
        runtime.release.set()
        _wait(lambda: queue.get(task_id).state == TaskState.FAILED)
        _wait(lambda: manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0)
    finally:
        runtime.release.set()
        backend.close()


def test_user_cancellation_releases_only_after_runtime_settles(tmp_path) -> None:
    runtime = BlockingRuntime()
    backend, queue, manager = _build(_store(tmp_path), runtime)
    try:
        backend.create_task({"command": "cancel runtime"})
        assert runtime.started.wait(1)
        task_id = queue.list_recent()[0].task_id
        assert backend.stop_agent({}).status == "accepted"
        _wait(lambda: queue.get(task_id).state == TaskState.CANCELLED)
        _wait(lambda: manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0)
        assert runtime.cancelled.is_set()
    finally:
        runtime.release.set()
        backend.close()


def test_cancel_waiting_task_removes_durable_wait_and_message(tmp_path) -> None:
    store = _store(tmp_path)
    backend, queue, manager = _build(store, BlockingRuntime())
    blocker = ResourceManager(store, Observer())
    assert blocker.request(scope=_SCOPE, owner_id=_OWNER, request_id="external").granted
    try:
        backend.create_task({"command": "waiting"})
        task_id = queue.list_recent()[0].task_id
        _wait(lambda: queue.get(task_id).state == TaskState.PAUSED)
        _wait(lambda: not backend._active_threads)
        assert backend.stop_agent({}).status == "completed"
        assert queue.get(task_id).state == TaskState.CANCELLED
        assert manager.queued(scope=_SCOPE, owner_id=_OWNER) == ()
        assert backend.snapshot()["tasks"][0]["resource_message"] == ""
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
    finally:
        blocker.release(scope=_SCOPE, owner_id=_OWNER, request_id="external")
        backend.close()


def test_saved_resume_waits_without_replaying_then_uses_same_checkpoint(tmp_path) -> None:
    store = _store(tmp_path)
    runtime = PausingRuntime()
    backend, queue, manager = _build(store, runtime)
    blocker = ResourceManager(store, Observer())
    try:
        backend.create_task({"command": "checkpointed"})
        task_id = queue.list_recent()[0].task_id
        _wait(lambda: queue.get(task_id).state == TaskState.PAUSED)
        _wait(lambda: not backend._active_threads)
        session = backend._coordinator.sessions.get(task_id)
        assert session is not None
        assert blocker.request(scope=_SCOPE, owner_id=_OWNER, request_id="external").granted
        backend.resume_task({})
        _wait(lambda: manager.queued(scope=_SCOPE, owner_id=_OWNER) == (task_id,))
        _wait(lambda: not backend._active_threads)
        assert queue.get(task_id).state == TaskState.PAUSED
        assert backend._coordinator.sessions.get(task_id) == session
        assert runtime.calls == 1
        assert runtime.resume_calls == 0

        blocker.release(scope=_SCOPE, owner_id=_OWNER, request_id="external")
        backend.resume_task({})
        _wait(lambda: queue.get(task_id).state == TaskState.COMPLETED)
        assert runtime.calls == 1
        assert runtime.resume_calls == 1
        _wait(lambda: manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0)
    finally:
        blocker.release(scope=_SCOPE, owner_id=_OWNER, request_id="external")
        backend.close()


def test_packaged_builder_injects_shared_store_and_real_owner_probe(tmp_path, monkeypatch) -> None:
    captured = []
    actual = nika_windows.DesktopBackend

    def capture(**kwargs):
        captured.append(kwargs)
        return actual(**kwargs)

    monkeypatch.setattr(nika_windows, "DesktopBackend", capture)
    config = AppConfig(database_path=tmp_path / "packaged.db")
    nika_windows.build_windows_bridge(config)
    nika_windows.build_windows_bridge(config)
    assert len(captured) == 2
    first, second = (item["resources"] for item in captured)
    assert first._store is captured[0]["queue"].store
    assert second._store is captured[1]["queue"].store
    assert first._store.path == second._store.path == config.database_path
    assert first._owner_probe is first._observer
    assert first._process_identity is not None
    assert first.request(scope=_SCOPE, owner_id=_OWNER, request_id="one").granted
    assert not second.request(scope=_SCOPE, owner_id=_OWNER, request_id="two").granted
    assert first.release(scope=_SCOPE, owner_id=_OWNER, request_id="one")


def test_cancelling_saved_resume_removes_its_fifo_waiter(tmp_path) -> None:
    store = _store(tmp_path)
    runtime = PausingRuntime()
    backend, queue, manager = _build(store, runtime)
    blocker = ResourceManager(store, Observer())
    try:
        backend.create_task({"command": "cancel checkpointed waiter"})
        task_id = queue.list_recent()[0].task_id
        _wait(lambda: queue.get(task_id).state == TaskState.PAUSED)
        _wait(lambda: not backend._active_threads)
        assert blocker.request(scope=_SCOPE, owner_id=_OWNER, request_id="external").granted
        backend.resume_task({})
        _wait(lambda: manager.queued(scope=_SCOPE, owner_id=_OWNER) == (task_id,))
        _wait(lambda: not backend._active_threads)
        assert backend.stop_agent({}).status == "accepted"
        _wait(lambda: queue.get(task_id).state == TaskState.CANCELLED)
        _wait(lambda: manager.queued(scope=_SCOPE, owner_id=_OWNER) == ())
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
        assert backend._coordinator.sessions.get(task_id) is None
        assert runtime.resume_calls == 0
    finally:
        blocker.release(scope=_SCOPE, owner_id=_OWNER, request_id="external")
        backend.close()
