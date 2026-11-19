from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import ResourceManager, ResourceSnapshot
from nika_core.runtime.contracts import RuntimeCapability, RuntimeOutcome, RuntimeResult
from scripts import nika_windows


class Observer:
    def snapshot(self):
        return ResourceSnapshot(10.0, 20.0, 8 * 1024**3)


class Runtime:
    runtime_id = 'packaged-schedule-integration'
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self, **_kwargs):
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
        raise AssertionError('scheduled startup cannot replay via resume')

    async def cancel(self, **_identity):
        self.finish.set()
        return True


def dispatch(bridge, action, payload):
    return bridge.dispatch({'request_id': 'packaged-schedule', 'action_id': action, 'payload': payload})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'Джерела зі пробілами'
    root.mkdir()
    a, b = root / 'Перше.txt', root / 'Друге.txt'
    a.write_text('Перше джерело', encoding='utf-8')
    b.write_text('Друге джерело', encoding='utf-8')
    config = AppConfig(database_path=tmp_path / 'Збережений розклад.db', v01_source_root=root, v01_source_a=a, v01_source_b=b)
    runtimes = []

    def runtime_factory(**kwargs):
        runtime = Runtime(**kwargs)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(nika_windows, 'PsutilResourceObserver', Observer)
    monkeypatch.setattr(nika_windows, 'V01PackagedThreeAgentRuntime', runtime_factory)
    bridges = []

    def build():
        bridge, _ = nika_windows.build_windows_bridge(config)
        bridges.append(bridge)
        return bridge

    try:
        yield config, build, runtimes
    finally:
        for runtime in runtimes:
            runtime.finish.set()
        for bridge in bridges:
            bridge._close()


def payload(delay=60):
    return {'command': 'Порівняй джерела', 'delay_seconds': delay, 'retry_interval_seconds': 1, 'start_window_seconds': 120}


def test_actual_builder_profile_save_restart_and_stale_editor(setup):
    config, build, _runtimes = setup
    first, stale = build(), build()
    assert first.get_state()['state']['resource_profile'] == {'profile': 'normal', 'revision': 1}
    assert dispatch(first, 'settings.resources.configure', {'profile': 'economy', 'revision': 1})['status'] == 'completed'
    assert dispatch(stale, 'settings.resources.configure', {'profile': 'low_memory', 'revision': 1})['status'] == 'rejected'
    first._close()
    restarted = build()
    assert restarted.get_state()['state']['resource_profile'] == {'profile': 'economy', 'revision': 2}
    store = SQLiteStore(config.database_path)
    budget = ResourceManager(store, Observer()).get_budget(scope='desktop_runtime', owner_id='local_machine')
    assert budget.max_cpu_percent == 75
    assert budget.max_memory_percent == 80
    assert dispatch(restarted, 'settings.resources.configure', {'profile': 'low_memory', 'revision': True})['status'] == 'rejected'


def test_actual_builder_delayed_dispatch_has_one_start_and_cancel_does_not_stop_live_runtime(setup):
    config, build, runtimes = setup
    bridge = build()
    result = dispatch(bridge, 'schedule.create', payload(delay=1))
    assert result['status'] == 'completed'
    assert runtimes[0].calls == 0
    assert runtimes[0].started.wait(4)
    schedules = bridge.get_state()['state']['schedules']
    assert len(schedules) == 1
    task_id = schedules[0]['task_id']
    assert dispatch(bridge, 'schedule.cancel', {'task_id': task_id})['status'] == 'completed'
    assert TaskQueue(SQLiteStore(config.database_path)).get(task_id).state is TaskState.RUNNING
    assert runtimes[0].calls == 1


def test_pending_schedule_survives_restart_and_can_be_cancelled_without_effects(setup):
    config, build, runtimes = setup
    bridge = build()
    assert dispatch(bridge, 'schedule.create', payload())['status'] == 'completed'
    task_id = bridge.get_state()['state']['schedules'][0]['task_id']
    bridge._close()
    restarted = build()
    state = restarted.get_state()['state']['schedules']
    assert [item['task_id'] for item in state] == [task_id]
    assert dispatch(restarted, 'schedule.pause', {'task_id': task_id})['status'] == 'completed'
    assert restarted.get_state()['state']['schedules'][0]['status'] == 'paused'
    assert dispatch(restarted, 'schedule.resume', {'task_id': task_id})['status'] == 'completed'
    assert dispatch(restarted, 'schedule.cancel', {'task_id': task_id})['status'] == 'completed'
    assert TaskQueue(SQLiteStore(config.database_path)).get(task_id).state is TaskState.CANCELLED
    assert all(runtime.calls == 0 for runtime in runtimes)


@pytest.mark.parametrize('field,value', [('delay_seconds', True), ('retry_interval_seconds', 1.5), ('start_window_seconds', 1)])
def test_invalid_schedule_cannot_prepare_a_task(setup, field, value):
    config, build, _ = setup
    bridge = build()
    request = {**payload(), field: value}
    assert dispatch(bridge, 'schedule.create', request)['status'] == 'rejected'
    assert TaskQueue(SQLiteStore(config.database_path)).list_recent() == ()


def test_profile_corruption_is_bounded_in_ui_and_blocks_new_runtime(setup):
    config, build, runtimes = setup
    bridge = build()
    store = SQLiteStore(config.database_path)
    with store.connection() as conn:
        conn.execute('INSERT INTO resource_profile_schema_migrations VALUES (2, ?)', ('PRIVATE_CANARY',))
    state = bridge.get_state()
    assert state['ok'] is True
    assert state['state']['resource_profile'] is None
    result = dispatch(bridge, 'settings.resources.configure', {'profile': 'normal', 'revision': 1})
    assert result['status'] == 'rejected'
    assert 'PRIVATE_CANARY' not in str([state, result])
    assert dispatch(bridge, 'task.create', {'command': 'Порівняй джерела'})['status'] == 'accepted'
    deadline = time.monotonic() + 2
    while TaskQueue(store).list_recent()[0].state is TaskState.READY and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert TaskQueue(store).list_recent()[0].state is TaskState.PAUSED
    assert runtimes[0].calls == 0
