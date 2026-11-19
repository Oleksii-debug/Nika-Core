from __future__ import annotations

import asyncio
import threading

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.resources import ResourceManager, ResourceSnapshot
from nika_core.runtime.contracts import RuntimeCapability, RuntimeOutcome, RuntimeResult
from nika_core.ui.desktop_backend import DesktopBackend


class Observer:
    def snapshot(self):
        return ResourceSnapshot(10.0, 20.0, 4 * 1024**3)


class Runtime:
    runtime_id = 'scheduled-admission'
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.finish = threading.Event()

    async def run(self, _request):
        self.calls += 1
        self.started.set()
        while not self.finish.is_set():
            await asyncio.sleep(0.005)
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, _request):
        raise AssertionError('scheduled admission cannot replay through resume')

    async def cancel(self, **_identity):
        self.finish.set()
        return True


def build(store, runtime):
    manager = ResourceManager(store, Observer())
    backend = DesktopBackend(
        queue=TaskQueue(store), agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store), audit=AuditLog(store),
        runtime=runtime, resources=manager,
    )
    return backend, manager, TaskQueue(store)


@pytest.fixture
def prepared(tmp_path):
    store = SQLiteStore(tmp_path / 'Заплановані завдання з пробілами.db')
    store.initialize()
    runtime = Runtime()
    backend, manager, queue = build(store, runtime)
    try:
        yield store, runtime, backend, manager, queue
    finally:
        runtime.finish.set()
        backend.close()


def test_preparation_never_starts_and_admission_ack_does_not_wait_for_completion(prepared):
    _store, runtime, backend, _manager, queue = prepared
    task = backend.prepare_scheduled_task({'command': 'Перевірити два джерела'})
    assert task.state is TaskState.READY
    assert runtime.calls == 0
    assert backend.start_scheduled_task(task.task_id) is True
    assert runtime.started.wait(1)
    assert queue.get(task.task_id).state is TaskState.RUNNING
    assert runtime.calls == 1
    assert backend.start_scheduled_task(task.task_id) is True
    assert runtime.started.wait(1)
    assert runtime.calls == 1
    assert backend.cancel_prepared_task(task.task_id) is False
    assert queue.get(task.task_id).state is TaskState.RUNNING


def test_resource_only_deferral_retries_same_never_started_task(prepared):
    _store, runtime, backend, manager, queue = prepared
    assert manager.request(scope='desktop_runtime', owner_id='local_machine', request_id='holder').granted
    task = backend.prepare_scheduled_task({'command': 'Безпечний відкладений запуск'})
    assert backend.start_scheduled_task(task.task_id) is False
    assert queue.get(task.task_id).state is TaskState.PAUSED
    assert runtime.calls == 0
    assert manager.release(scope='desktop_runtime', owner_id='local_machine', request_id='holder')
    assert backend.start_scheduled_task(task.task_id) is True
    assert runtime.started.wait(1)
    assert runtime.calls == 1


def test_cancel_never_started_task_clears_waiter_and_prevents_dispatch(prepared):
    _store, runtime, backend, manager, queue = prepared
    assert manager.request(scope='desktop_runtime', owner_id='local_machine', request_id='holder').granted
    task = backend.prepare_scheduled_task({'command': 'Скасований запуск'})
    assert backend.start_scheduled_task(task.task_id) is False
    assert backend.cancel_prepared_task(task.task_id) is True
    assert task.task_id not in manager.queued(scope='desktop_runtime', owner_id='local_machine')
    assert backend.start_scheduled_task(task.task_id) is True
    assert queue.get(task.task_id).state is TaskState.CANCELLED
    assert runtime.calls == 0


def test_user_pause_is_not_a_resource_retry(prepared):
    _store, runtime, backend, _manager, queue = prepared
    task = backend.prepare_scheduled_task({'command': 'Ручна пауза'})
    queue.transition(task.task_id, TaskState.PAUSED)
    with pytest.raises(ValueError, match='користувачем'):
        backend.start_scheduled_task(task.task_id)
    assert runtime.calls == 0
    assert queue.get(task.task_id).state is TaskState.PAUSED


def test_restart_does_not_replay_existing_durable_running_history(prepared):
    store, runtime, backend, _manager, _queue = prepared
    task = backend.prepare_scheduled_task({'command': 'Запуск до відновлення'})
    assert backend.start_scheduled_task(task.task_id) is True
    assert runtime.started.wait(1)
    other_runtime = Runtime()
    other, _manager, _queue = build(SQLiteStore(store.path), other_runtime)
    try:
        assert other.start_scheduled_task(task.task_id) is True
        assert other_runtime.calls == 0
    finally:
        other_runtime.finish.set()
        other.close()
    assert runtime.calls == 1


def test_foreign_task_and_missing_command_do_not_get_effects(prepared):
    _store, runtime, backend, _manager, queue = prepared
    task = queue.create(workspace_id='foreign', agent_id='nika.default', payload={'command': 'x'})
    queue.transition(task.task_id, TaskState.READY)
    with pytest.raises(ValueError, match='іншого'):
        backend.start_scheduled_task(task.task_id)
    assert runtime.calls == 0


def test_second_backend_cannot_pause_the_owner_during_admission(prepared):
    from concurrent.futures import ThreadPoolExecutor

    store, runtime, first, manager, queue = prepared
    other_runtime = Runtime()
    second, _other_manager, _queue = build(SQLiteStore(store.path), other_runtime)
    task = first.prepare_scheduled_task({'command': 'Один запуск для двох екземплярів'})
    entered = threading.Event()
    release = threading.Event()
    original = first._coordinator.start

    async def gated(*args, **kwargs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return await original(*args, **kwargs)

    first._coordinator.start = gated
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first.start_scheduled_task, task.task_id)
            try:
                assert entered.wait(2)
                with pytest.raises(RuntimeError, match='підтверджено'):
                    second.start_scheduled_task(task.task_id)
                assert queue.get(task.task_id).state is TaskState.READY
                assert manager.active_count(scope='desktop_runtime', owner_id='local_machine') == 1
                assert runtime.calls == other_runtime.calls == 0
            finally:
                release.set()
            assert future.result(timeout=3) is True
        assert runtime.started.wait(1)
        assert second.start_scheduled_task(task.task_id) is True
        assert runtime.calls == 1
        assert other_runtime.calls == 0
    finally:
        release.set()
        other_runtime.finish.set()
        second.close()
