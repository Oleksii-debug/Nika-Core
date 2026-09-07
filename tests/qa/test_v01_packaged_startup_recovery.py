from __future__ import annotations

from threading import Event

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.session_store import RuntimeSessionStore
from scripts import nika_windows


class _PackagedRecoveryRuntime:
    runtime_id = "qa.packaged-startup-recovery"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.resume_started = Event()
        self.resume_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        return f"qa:{task_id}:{thread_id}"

    async def run(self, request):
        raise AssertionError(f"startup recovery must resume, not fresh-run {request.task_id}")

    async def resume(self, request):
        self.resume_calls += 1
        self.resume_started.set()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"recovered_task_id": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        expected = self.initial_resume_token(task_id=task_id, thread_id=thread_id)
        if resume_token != expected:
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.INVALID,
                reason="qa cursor mismatch",
            )
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="qa checkpoint is readable",
            checkpoint_id=f"qa-checkpoint:{task_id}:{thread_id}",
        )


def _crash_left_running_task(path, runtime: _PackagedRecoveryRuntime):
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "resume the exact crash-left packaged task"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = f"desktop-{task.task_id}"
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token=runtime.initial_resume_token(
            task_id=task.task_id,
            thread_id=thread_id,
        ),
    )
    return store, queue, task.task_id


def _run_packaged_entrypoint(monkeypatch, config: AppConfig, runtime, launch_assertion) -> None:
    monkeypatch.setattr(
        nika_windows,
        "V01PackagedThreeAgentRuntime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(
        nika_windows.AppConfig,
        "from_environment",
        staticmethod(lambda: config),
    )

    def fake_launch(bridge, *, title):
        assert title.startswith("Nika Core ")
        launch_assertion(bridge)

    monkeypatch.setattr(nika_windows, "launch_windows_shell", fake_launch)
    result = nika_windows.main([])
    assert result in {0, None}


def test_packaged_reopen_routes_crash_left_running_session_through_canonical_recovery(
    tmp_path, monkeypatch
) -> None:
    """A recreated packaged process must not leave stale RUNNING truth."""

    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка restart oracle" / "nika core.db"
    _store, queue, task_id = _crash_left_running_task(database, runtime)
    config = AppConfig(database_path=database)

    def assert_during_launch(bridge) -> None:
        assert runtime.resume_started.wait(timeout=2), (
            "Packaged startup never entered canonical runtime recovery for a crash-left "
            "ACTIVE/RUNNING session."
        )
        state = bridge.get_state()["state"]
        task = next(item for item in state["tasks"] if item["task_id"] == task_id)
        assert task["state"] != TaskState.RUNNING.value or runtime.resume_calls == 1

    _run_packaged_entrypoint(monkeypatch, config, runtime, assert_during_launch)

    assert runtime.resume_calls == 1
    assert queue.get(task_id).state is TaskState.COMPLETED
    assert RuntimeSessionStore(SQLiteStore(database)).get(task_id) is None


def test_packaged_reopen_promotes_unresolved_effect_to_uncertain_and_never_resumes(
    tmp_path, monkeypatch
) -> None:
    """Restart reconciliation must run before presenting stale RUNNING as live work."""

    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка uncertain oracle" / "nika core.db"
    store, queue, task_id = _crash_left_running_task(database, runtime)
    ledger = IdempotencyLedger(store)
    operation_key = "qa:external-effect:before-crash"
    ledger.reserve(
        operation_key=operation_key,
        task_id=task_id,
        operation_type="qa.external_effect",
        input_fingerprint="sha256:qa-fixed-input",
    )

    marked_uncertain = Event()
    original = IdempotencyLedger.mark_uncertain

    def mark_uncertain(self, key: str):
        result = original(self, key)
        if key == operation_key:
            marked_uncertain.set()
        return result

    monkeypatch.setattr(IdempotencyLedger, "mark_uncertain", mark_uncertain)
    config = AppConfig(database_path=database)

    def assert_during_launch(bridge) -> None:
        assert marked_uncertain.wait(timeout=2), (
            "Packaged startup did not inventory/promote the crash-left external effect "
            "before exposing the recovered application."
        )
        state = bridge.get_state()["state"]
        task = next(item for item in state["tasks"] if item["task_id"] == task_id)
        assert task["state"] == TaskState.RUNNING.value

    _run_packaged_entrypoint(monkeypatch, config, runtime, assert_during_launch)

    assert runtime.resume_calls == 0
    assert queue.get(task_id).state is TaskState.RUNNING
    assert ledger.require(operation_key).status is IdempotencyStatus.UNCERTAIN
