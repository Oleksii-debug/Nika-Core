from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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


class _StaticHealthProbe:
    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> object:
        self.calls += 1
        if isinstance(self._snapshot, Exception):
            raise self._snapshot
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


class _BehavioralFactory:
    def __init__(self, probe: _StaticHealthProbe) -> None:
        self._probe = probe
        self.calls = 0

    def __bool__(self) -> bool:
        raise AssertionError("health factory truthiness must not execute")

    def __call__(self, _selection: ModelSelection) -> _StaticHealthProbe:
        self.calls += 1
        return self._probe


class _ExplodingModelFactory:
    def __init__(self) -> None:
        self.calls = 0

    def for_task(self, task_id: str) -> None:
        self.calls += 1
        raise AssertionError(f"model runtime must not be built during recovery probe: {task_id}")


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _runtime(
    store: SQLiteStore,
    *,
    health_probe_factory=None,
    model_runtime_factory=None,
) -> V01PackagedThreeAgentRuntime:
    return V01PackagedThreeAgentRuntime(
        store=store,
        config=AppConfig(database_path=store.path),
        ollama_health_probe_factory=health_probe_factory,
        model_runtime_factory=model_runtime_factory,
    )


def _task_with_selection(
    store: SQLiteStore,
    selection: dict[str, object],
) -> str:
    settings = V01ModelSettings(store)
    result = settings.configure({"revision": 0, **selection})
    assert result.status == "completed"
    payload = settings.prepare_task_payload({"command": "resume exact frozen task"})
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    )
    return task.task_id


def _legacy_task(store: SQLiteStore) -> str:
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "legacy deterministic task"},
    )
    return task.task_id


def _deterministic() -> dict[str, object]:
    return {
        "route_kind": "deterministic",
        "provider_id": None,
        "model": None,
        "base_url": None,
        "credential_ref": None,
        "private_data_allowed": True,
        "timeout_seconds": 60.0,
    }


def _ollama() -> dict[str, object]:
    return {
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "local-model:1",
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": True,
        "timeout_seconds": 60.0,
    }


def _foundry() -> dict[str, object]:
    return {
        "route_kind": "foundry_local",
        "provider_id": "foundry-local",
        "model": "embedded-model",
        "base_url": None,
        "credential_ref": None,
        "private_data_allowed": True,
        "timeout_seconds": 60.0,
    }


def _api() -> dict[str, object]:
    return {
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": "api-model",
        "base_url": "https://api.example.test",
        "credential_ref": "env:NIKA_TEST_API_KEY",
        "private_data_allowed": False,
        "timeout_seconds": 60.0,
    }


def _probe(runtime: V01PackagedThreeAgentRuntime, task_id: str, token: str | None = None):
    thread_id = f"desktop-{task_id}"
    resume_token = token or runtime.initial_resume_token(
        task_id=task_id,
        thread_id=thread_id,
    )
    return asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=resume_token,
        )
    )


def _snapshot(
    *,
    reachable: ModelHealthFact,
    present: ModelHealthFact,
    ready: ModelHealthFact,
) -> ModelHealthSnapshot:
    return ModelHealthSnapshot(
        configured=ModelHealthFact.YES,
        reachable=reachable,
        model_present=present,
        model_ready=ready,
        inference_proven=ModelHealthFact.UNKNOWN,
    )


def test_invalid_cursor_performs_zero_model_health_effects(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    factory_calls: list[ModelSelection] = []

    def forbidden(selection: ModelSelection):
        factory_calls.append(selection)
        raise AssertionError("invalid cursor must stop before model health")

    runtime = _runtime(store, health_probe_factory=forbidden)

    result = _probe(runtime, task_id, token="wrong-cursor")

    assert result.status is RuntimeResumeProbeStatus.INVALID
    assert factory_calls == []


def test_missing_durable_task_never_becomes_ready_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    factory_calls: list[ModelSelection] = []

    def forbidden(selection: ModelSelection):
        factory_calls.append(selection)
        raise AssertionError("missing task must stop before model health")

    runtime = _runtime(store, health_probe_factory=forbidden)
    task_id = "missing-task"
    thread_id = f"desktop-{task_id}"
    resume_token = runtime.initial_resume_token(
        task_id=task_id,
        thread_id=thread_id,
    )

    result = asyncio.run(
        runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=resume_token,
        )
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert factory_calls == []


@pytest.mark.parametrize("selection", [_deterministic()])
def test_explicit_deterministic_recovery_preserves_ready_contract(
    tmp_path: Path,
    selection: dict[str, object],
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, selection)

    def forbidden(_selection: ModelSelection):
        raise AssertionError("deterministic recovery must not probe model health")

    result = _probe(_runtime(store, health_probe_factory=forbidden), task_id)

    assert result.status is RuntimeResumeProbeStatus.READY
    assert result.checkpoint_id is not None


def test_legacy_unbound_recovery_remains_deterministic_and_ready(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _legacy_task(store)

    def forbidden(_selection: ModelSelection):
        raise AssertionError("legacy deterministic recovery must not probe model health")

    result = _probe(_runtime(store, health_probe_factory=forbidden), task_id)

    assert result.status is RuntimeResumeProbeStatus.READY


def test_recovery_health_uses_frozen_task_route_after_default_changes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    settings = V01ModelSettings(store)
    assert settings.configure({"revision": 1, **_deterministic()}).status == "completed"
    health = _StaticHealthProbe(
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.YES,
            ready=ModelHealthFact.YES,
        )
    )
    seen: list[ModelSelection] = []

    def factory(selection: ModelSelection):
        seen.append(selection)
        return health

    result = _probe(_runtime(store, health_probe_factory=factory), task_id)

    assert result.status is RuntimeResumeProbeStatus.READY
    assert [selection.route_kind for selection in seen] == ["ollama"]
    assert seen[0].model == "local-model:1"
    assert seen[0].base_url == "http://localhost:11434"
    assert settings.snapshot()["route_kind"] == "deterministic"


def test_ready_ollama_route_allows_exact_crash_recovery_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    health = _StaticHealthProbe(
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.YES,
            ready=ModelHealthFact.YES,
        )
    )
    seen: list[ModelSelection] = []

    def factory(selection: ModelSelection):
        seen.append(selection)
        return health

    result = _probe(_runtime(store, health_probe_factory=factory), task_id)

    assert result.status is RuntimeResumeProbeStatus.READY
    assert result.checkpoint_id is not None
    assert health.calls == 1
    assert [selection.route_kind for selection in seen] == ["ollama"]
    assert seen[0].model == "local-model:1"
    assert seen[0].base_url == "http://localhost:11434"


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(
            reachable=ModelHealthFact.NO,
            present=ModelHealthFact.UNKNOWN,
            ready=ModelHealthFact.UNKNOWN,
        ),
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.NO,
            ready=ModelHealthFact.NO,
        ),
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.YES,
            ready=ModelHealthFact.UNKNOWN,
        ),
    ],
)
def test_nonready_ollama_health_blocks_auto_resume_before_model_runtime(
    tmp_path: Path,
    snapshot: ModelHealthSnapshot,
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    health = _StaticHealthProbe(snapshot)
    model_factory = _ExplodingModelFactory()

    result = _probe(
        _runtime(
            store,
            health_probe_factory=lambda _selection: health,
            model_runtime_factory=model_factory,
        ),
        task_id,
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert health.calls == 1
    assert model_factory.calls == 0


@pytest.mark.parametrize("selection", [_foundry(), _api()])
def test_routes_without_canonical_recovery_health_never_gain_ready_authority(
    tmp_path: Path,
    selection: dict[str, object],
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, selection)
    factory_calls: list[ModelSelection] = []
    model_factory = _ExplodingModelFactory()

    def forbidden(model_selection: ModelSelection):
        factory_calls.append(model_selection)
        raise AssertionError("unsupported route must not reuse the Ollama health adapter")

    result = _probe(
        _runtime(
            store,
            health_probe_factory=forbidden,
            model_runtime_factory=model_factory,
        ),
        task_id,
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert factory_calls == []
    assert model_factory.calls == 0


def test_health_factory_selection_does_not_execute_caller_truthiness(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    health = _StaticHealthProbe(
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.YES,
            ready=ModelHealthFact.YES,
        )
    )
    factory = _BehavioralFactory(health)

    result = _probe(_runtime(store, health_probe_factory=factory), task_id)

    assert result.status is RuntimeResumeProbeStatus.READY
    assert factory.calls == 1
    assert health.calls == 1


def test_noncanonical_health_carrier_is_rejected_before_attribute_behavior(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    accesses: list[str] = []
    health = _StaticHealthProbe(_BehavioralSnapshot(accesses))

    result = _probe(
        _runtime(store, health_probe_factory=lambda _selection: health),
        task_id,
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert accesses == []
    assert health.calls == 1


@pytest.mark.parametrize(
    "observed",
    [
        RuntimeError("health unavailable"),
        object(),
    ],
)
def test_health_probe_failure_or_malformed_snapshot_blocks_auto_resume(
    tmp_path: Path,
    observed: object,
) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    health = _StaticHealthProbe(observed)

    result = _probe(
        _runtime(store, health_probe_factory=lambda _selection: health),
        task_id,
    )

    assert result.status is RuntimeResumeProbeStatus.UNVERIFIABLE
    assert result.checkpoint_id is None
    assert health.calls == 1


def test_recovery_service_stops_unready_model_before_auto_resume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_with_selection(store, _ollama())
    queue = TaskQueue(store)
    queue.transition(task_id, TaskState.READY)
    queue.transition(task_id, TaskState.RUNNING)
    health = _StaticHealthProbe(
        _snapshot(
            reachable=ModelHealthFact.YES,
            present=ModelHealthFact.YES,
            ready=ModelHealthFact.UNKNOWN,
        )
    )
    model_factory = _ExplodingModelFactory()
    runtime = _runtime(
        store,
        health_probe_factory=lambda _selection: health,
        model_runtime_factory=model_factory,
    )
    thread_id = f"desktop-{task_id}"
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
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
    audit = AuditLog(store)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=audit,
        runtimes=registry,
        sessions=sessions,
    )

    executions = asyncio.run(recovery.resume_safe_crash_sessions(max_count=1))

    assert len(executions) == 1
    execution = executions[0]
    assert execution.candidate.disposition is RecoveryDisposition.CHECKPOINT_UNAVAILABLE
    assert execution.result is None
    assert execution.error is not None
    assert queue.get(task_id).state is TaskState.RUNNING
    assert sessions.get(task_id) is not None
    assert health.calls == 1
    assert model_factory.calls == 0
    event_types = [
        event.event_type
        for event in audit.list_for(entity_type="task", entity_id=task_id)
    ]
    assert "runtime.recovery_checkpoint_blocked" in event_types
    assert "runtime.recovery_auto_resume_requested" not in event_types
