from __future__ import annotations

import asyncio
import json

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.diagnostics import ModelHealthFact, ModelHealthSnapshot
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import RuntimeResumeProbeStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.v01_model_settings import ModelSelection, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime


class _FixedHealthProbe:
    def __init__(self, snapshot: ModelHealthSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> ModelHealthSnapshot:
        self.calls += 1
        return self._snapshot


class _HealthFactory:
    def __init__(self, snapshot: ModelHealthSnapshot) -> None:
        self.probe = _FixedHealthProbe(snapshot)
        self.selections: list[ModelSelection] = []

    def __call__(self, selection: ModelSelection) -> _FixedHealthProbe:
        self.selections.append(selection)
        return self.probe


class _UnexpectedHealthFactory:
    def __call__(self, selection: ModelSelection) -> _FixedHealthProbe:
        raise AssertionError(
            f"health probe must not be constructed for route {selection.route_kind}"
        )


class _BehavioralHealthFactory:
    def __init__(self, probe: _FixedHealthProbe) -> None:
        self.probe = probe
        self.calls = 0

    def __bool__(self) -> bool:
        raise AssertionError("health factory truthiness must not execute")

    def __call__(self, _selection: ModelSelection) -> _FixedHealthProbe:
        self.calls += 1
        return self.probe


class _BehavioralDependency:
    def __init__(self, name: str) -> None:
        self.name = name
        self.truthiness_calls = 0

    def __bool__(self) -> bool:
        self.truthiness_calls += 1
        raise AssertionError(f"{self.name} truthiness must not execute")


class _RawHealthProbe:
    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> object:
        self.calls += 1
        return self._snapshot


class _BehavioralSnapshot:
    def __init__(self, accesses: list[str]) -> None:
        object.__setattr__(self, "_accesses", accesses)

    def __getattribute__(self, name: str):
        if name in {
            "configured",
            "reachable",
            "model_present",
            "model_ready",
            "inference_proven",
        }:
            accesses = object.__getattribute__(self, "_accesses")
            accesses.append(name)
            raise AssertionError("noncanonical health carrier behavior must not execute")
        return object.__getattribute__(self, name)


class _TrackingRuntime(V01PackagedThreeAgentRuntime):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.resume_calls = 0

    async def resume(self, request):  # type: ignore[no-untyped-def]
        self.resume_calls += 1
        raise AssertionError(f"blocked recovery must not resume {request.task_id}")


def _snapshot(*, ready: ModelHealthFact) -> ModelHealthSnapshot:
    return ModelHealthSnapshot(
        configured=ModelHealthFact.YES,
        reachable=ModelHealthFact.YES,
        model_present=ModelHealthFact.YES,
        model_ready=ready,
        inference_proven=ModelHealthFact.UNKNOWN,
    )


def _task_with_selection(
    store: SQLiteStore,
    *,
    payload: dict[str, object],
) -> tuple[V01ModelSettings, TaskQueue, str]:
    settings = V01ModelSettings(store)
    configured = settings.configure({"revision": 0, **payload})
    assert configured.status == "completed"
    frozen = settings.prepare_task_payload({"command": "resume health regression"})
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload=frozen,
    )
    return settings, queue, task.task_id


def _runtime(
    store: SQLiteStore,
    settings: V01ModelSettings,
    health_factory,
) -> V01PackagedThreeAgentRuntime:
    return V01PackagedThreeAgentRuntime(
        store=store,
        config=AppConfig(database_path=store.path),
        model_settings=settings,
        model_health_probe_factory=health_factory,
    )


def test_deterministic_resume_probe_requires_no_model_health(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "deterministic" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "deterministic",
            "provider_id": None,
            "model": None,
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    runtime = _runtime(store, settings, _UnexpectedHealthFactory())
    thread_id = f"desktop-{task_id}"

    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.READY


def test_ollama_resume_probe_requires_exact_route_health_ready(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ollama-ready" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    health = _HealthFactory(_snapshot(ready=ModelHealthFact.YES))
    runtime = _runtime(store, settings, health)
    thread_id = f"desktop-{task_id}"

    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.READY
    assert health.probe.calls == 1
    assert len(health.selections) == 1
    assert health.selections[0].provider_id == "ollama"
    assert health.selections[0].model == "qwen3:8b"
    assert health.selections[0].base_url == "http://localhost:11434"


def test_constructor_dependencies_do_not_execute_caller_truthiness(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "dependency-truthiness" / "nika.db")
    store.initialize()
    source_settings = _BehavioralDependency("source settings")
    model_settings = _BehavioralDependency("model settings")
    model_factory = _BehavioralDependency("model runtime factory")

    V01PackagedThreeAgentRuntime(
        store=store,
        config=AppConfig(database_path=store.path),
        source_settings=source_settings,  # type: ignore[arg-type]
        model_settings=model_settings,  # type: ignore[arg-type]
        model_runtime_factory=model_factory,  # type: ignore[arg-type]
    )

    assert source_settings.truthiness_calls == 0
    assert model_settings.truthiness_calls == 0
    assert model_factory.truthiness_calls == 0


def test_health_factory_selection_does_not_execute_caller_truthiness(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "factory-truthiness" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    probe = _FixedHealthProbe(_snapshot(ready=ModelHealthFact.YES))
    factory = _BehavioralHealthFactory(probe)
    runtime = _runtime(store, settings, factory)
    thread_id = f"desktop-{task_id}"

    result = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert result.status is RuntimeResumeProbeStatus.READY
    assert factory.calls == 1
    assert probe.calls == 1


def test_noncanonical_health_carrier_is_rejected_before_attribute_behavior(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "behavioral-snapshot" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    accesses: list[str] = []
    probe = _RawHealthProbe(_BehavioralSnapshot(accesses))
    runtime = _runtime(store, settings, lambda _selection: probe)
    thread_id = f"desktop-{task_id}"

    result = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert accesses == []
    assert probe.calls == 1


def test_mutated_exact_health_snapshot_is_revalidated_before_ready(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mutated-snapshot" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    snapshot = _snapshot(ready=ModelHealthFact.YES)
    object.__setattr__(snapshot, "reachable", ModelHealthFact.NO)
    health = _HealthFactory(snapshot)
    runtime = _runtime(store, settings, health)
    thread_id = f"desktop-{task_id}"

    result = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert health.probe.calls == 1


def test_foundry_resume_remains_unverifiable_without_route_health_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "foundry-blocked" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "foundry_local",
            "provider_id": "foundry-local",
            "model": "embedded-test-model",
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    runtime = _runtime(store, settings, _UnexpectedHealthFactory())
    thread_id = f"desktop-{task_id}"

    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert probe.checkpoint_id is None


def test_unready_ollama_route_blocks_recovery_before_runtime_resume(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ollama-recovery-blocked" / "nika.db")
    store.initialize()
    settings, queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    queue.transition(task_id, TaskState.READY)
    queue.transition(task_id, TaskState.RUNNING)
    health = _HealthFactory(_snapshot(ready=ModelHealthFact.UNKNOWN))
    runtime = _TrackingRuntime(
        store=store,
        config=AppConfig(database_path=store.path),
        model_settings=settings,
        model_health_probe_factory=health,
    )
    thread_id = f"desktop-{task_id}"
    RuntimeSessionStore(store).record_active(
        task_id=task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token=runtime.initial_resume_token(
            task_id=task_id,
            thread_id=thread_id,
        ),
    )
    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
    )

    executions = asyncio.run(recovery.resume_safe_crash_sessions())

    assert len(executions) == 1
    assert executions[0].candidate.disposition is RecoveryDisposition.CHECKPOINT_UNAVAILABLE
    assert executions[0].result is None
    assert runtime.resume_calls == 0
    assert health.probe.calls == 1
    assert queue.get(task_id).state is TaskState.RUNNING


def _remove_model_reference(store: SQLiteStore, task_id: str) -> None:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert isinstance(payload, dict)
        assert payload.pop("v01_model_selection", None) is not None
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (json.dumps(payload, sort_keys=True), task_id),
        )


def test_bound_model_task_cannot_downgrade_to_legacy_after_reference_loss(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bound-reference-loss" / "nika.db")
    store.initialize()
    settings, _queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    assert settings.for_task(task_id).model == "qwen3:8b"
    _remove_model_reference(store, task_id)
    runtime = _runtime(store, settings, _UnexpectedHealthFactory())
    thread_id = f"desktop-{task_id}"

    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert probe.checkpoint_id is None


def test_true_legacy_task_without_selection_or_binding_keeps_no_model_resume(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "legacy-no-model" / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "historical deterministic task"},
    )
    settings = V01ModelSettings(store)
    runtime = _runtime(store, settings, _UnexpectedHealthFactory())
    thread_id = f"desktop-{task.task_id}"

    probe = asyncio.run(
        runtime.probe_resume(
            task_id=task.task_id,
            thread_id=thread_id,
            resume_token=runtime.initial_resume_token(
                task_id=task.task_id,
                thread_id=thread_id,
            ),
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.READY

def test_bound_model_reference_loss_blocks_startup_recovery_execution(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bound-reference-loss-recovery" / "nika.db")
    store.initialize()
    settings, queue, task_id = _task_with_selection(
        store,
        payload={
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60,
        },
    )
    assert settings.for_task(task_id).model == "qwen3:8b"
    queue.transition(task_id, TaskState.READY)
    queue.transition(task_id, TaskState.RUNNING)
    _remove_model_reference(store, task_id)

    runtime = _TrackingRuntime(
        store=store,
        config=AppConfig(database_path=store.path),
        model_settings=settings,
        model_health_probe_factory=_UnexpectedHealthFactory(),
    )
    thread_id = f"desktop-{task_id}"
    RuntimeSessionStore(store).record_active(
        task_id=task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token=runtime.initial_resume_token(
            task_id=task_id,
            thread_id=thread_id,
        ),
    )
    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
    )

    executions = asyncio.run(recovery.resume_safe_crash_sessions())

    assert len(executions) == 1
    assert executions[0].candidate.disposition is RecoveryDisposition.CHECKPOINT_UNAVAILABLE
    assert executions[0].result is None
    assert runtime.resume_calls == 0
    assert queue.get(task_id).state is TaskState.RUNNING

