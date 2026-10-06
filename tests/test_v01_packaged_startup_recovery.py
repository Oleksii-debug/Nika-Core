from __future__ import annotations

from threading import Event

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows


class _PackagedRecoveryRuntime:
    runtime_id = "test.packaged-startup-recovery"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.resume_started = Event()
        self.resume_calls = 0
        self.resumed_task_ids: list[str] = []

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        return f"test:{task_id}:{thread_id}"

    async def run(self, request):
        raise AssertionError(
            f"startup recovery must resume, not fresh-run {request.task_id}"
        )

    async def resume(self, request):
        self.resume_calls += 1
        self.resumed_task_ids.append(request.task_id)
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
                reason="test cursor mismatch",
            )
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="test checkpoint is readable",
            checkpoint_id=f"test-checkpoint:{task_id}:{thread_id}",
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

    def fake_launch(bridge, *, title, on_gui_started=None):
        assert title.startswith("Nika Core ")
        assert on_gui_started is not None
        on_gui_started()
        launch_assertion(bridge)

    monkeypatch.setattr(nika_windows, "launch_windows_shell", fake_launch)
    result = nika_windows.main([])
    assert result in {0, None}


def test_packaged_reopen_resumes_only_checkpoint_validated_crash_left_work(
    tmp_path, monkeypatch
) -> None:
    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка restart regression" / "nika core.db"
    _store, queue, task_id = _crash_left_running_task(database, runtime)
    config = AppConfig(database_path=database)

    def assert_during_launch(bridge) -> None:
        assert runtime.resume_started.wait(timeout=2), (
            "Packaged startup did not enter canonical recovery before shell exposure."
        )
        state = bridge.get_state()["state"]
        task = next(item for item in state["tasks"] if item["task_id"] == task_id)
        assert task["state"] != TaskState.RUNNING.value or runtime.resume_calls == 1
        recovery = state["startup_recovery"]
        assert recovery["status"] in {"recovering", "ready"}
        assert recovery["uncertain_count"] == 0

    _run_packaged_entrypoint(monkeypatch, config, runtime, assert_during_launch)

    assert runtime.resume_calls == 1
    assert queue.get(task_id).state is TaskState.COMPLETED
    assert RuntimeSessionStore(SQLiteStore(database)).get(task_id) is None


def test_packaged_reopen_promotes_unresolved_effect_before_shell_and_never_resumes(
    tmp_path, monkeypatch
) -> None:
    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка uncertain regression" / "nika core.db"
    store, queue, task_id = _crash_left_running_task(database, runtime)
    ledger = IdempotencyLedger(store)
    operation_key = "test:external-effect:before-crash"
    ledger.reserve(
        operation_key=operation_key,
        task_id=task_id,
        operation_type="test.external_effect",
        input_fingerprint="sha256:test-fixed-input",
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
            "Packaged startup exposed shell before unresolved effect reconciliation."
        )
        state = bridge.get_state()["state"]
        task = next(item for item in state["tasks"] if item["task_id"] == task_id)
        assert task["state"] == TaskState.RUNNING.value
        recovery = state["startup_recovery"]
        assert recovery["status"] == "attention"
        assert recovery["uncertain_count"] == 1
        assert recovery["auto_resume_count"] == 0

    _run_packaged_entrypoint(monkeypatch, config, runtime, assert_during_launch)

    assert runtime.resume_calls == 0
    assert queue.get(task_id).state is TaskState.RUNNING
    assert ledger.require(operation_key).status is IdempotencyStatus.UNCERTAIN


def test_startup_recovery_admission_rejection_demotes_to_manual_resume(
    tmp_path,
) -> None:
    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка denied recovery" / "nika core.db"
    store, queue, task_id = _crash_left_running_task(database, runtime)
    admission_calls: list[str] = []

    def reject_recovery(record) -> None:
        admission_calls.append(record.task_id)
        raise PermissionError("test recovery authority denied")

    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
        admit_recovered_task=reject_recovery,
    )

    recovery = backend.start_startup_recovery(startup_wait_seconds=0)

    assert admission_calls == [task_id]
    assert runtime.resume_calls == 0
    assert queue.get(task_id).state is TaskState.PAUSED
    assert recovery["status"] == "manual"
    assert recovery["auto_resume_count"] == 0
    assert recovery["manual_resume_count"] == 1
    assert recovery["resume_failed_count"] == 0
    backend.close()


def test_startup_recovery_admission_rejection_isolated_per_task(
    tmp_path,
) -> None:
    runtime = _PackagedRecoveryRuntime()
    database = tmp_path / "Ніка mixed recovery" / "nika core.db"
    store, queue, rejected_id = _crash_left_running_task(database, runtime)
    _store2, queue2, approved_id = _crash_left_running_task(database, runtime)
    assert queue2.get(approved_id).state is TaskState.RUNNING

    def admit_recovery(record) -> None:
        if record.task_id == rejected_id:
            raise PermissionError("test one-task authority denial")

    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
        admit_recovered_task=admit_recovery,
    )

    backend.start_startup_recovery(startup_wait_seconds=2)

    assert queue.get(rejected_id).state is TaskState.PAUSED
    assert queue.get(approved_id).state is TaskState.COMPLETED
    assert runtime.resumed_task_ids == [approved_id]
    for _ in range(100):
        recovery = backend.startup_recovery_snapshot()
        if recovery["status"] == "manual":
            break
        Event().wait(0.01)
    assert recovery["status"] == "manual"
    assert recovery["auto_resume_count"] == 0
    assert recovery["manual_resume_count"] == 1
    assert recovery["resume_failed_count"] == 0
    backend.close()

