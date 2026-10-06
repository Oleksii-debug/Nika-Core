from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class _BarrierLocalProvider:
    def __init__(
        self,
        entered: asyncio.Event,
        release: asyncio.Event,
        *,
        supports_hard_cancellation: bool = False,
    ) -> None:
        self._entered = entered
        self._release = release
        self._supports_hard_cancellation = supports_hard_cancellation

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=self._supports_hard_cancellation,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._entered.set()
        await self._release.wait()
        return ModelResponse(
            request_id=request.request_id,
            text="deterministic embedded result",
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


def _definitions(store: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Complete the assigned task.",
        instructions="Return deterministic fixture evidence.",
        model_profile="configured",
    )
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


def _runtime(
    *, gateway: ModelGateway, definitions: AgentDefinitionRepository
) -> ModelGatewayAgentRuntime:
    return ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="foundry-local",
        provider_kind=ProviderKind.LOCAL,
        model="fixture-model",
    )


def _request(task_id: str) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id="thread-foundry-crash",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "deterministic crash-window fixture"},
        },
    )


def test_inflight_model_request_is_durable_but_never_claims_resumable_checkpoint(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "nika.db")
        store.initialize()
        queue = TaskQueue(store)
        audit = AuditLog(store)
        sessions = RuntimeSessionStore(store)
        definitions = _definitions(store)

        entered = asyncio.Event()
        release = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(_BarrierLocalProvider(entered, release), default=True)
        runtime = _runtime(gateway=gateway, definitions=definitions)
        coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)

        task = queue.create(workspace_id="proof", agent_id="worker")
        queue.transition(task.task_id, TaskState.READY)
        execution = asyncio.create_task(coordinator.start(runtime, _request(task.task_id)))

        try:
            await entered.wait()
            persisted = sessions.get(task.task_id)
            assert persisted is not None
            assert persisted.is_active
            assert persisted.runtime_id == runtime.runtime_id
            assert persisted.thread_id == "thread-foundry-crash"
            assert persisted.resume_token.startswith("model-gateway-inflight-v1:")
            assert RuntimeCapability.DURABLE_RESUME not in runtime.capabilities
            assert RuntimeCapability.CANCELLATION not in runtime.capabilities

            restarted_runtime = _runtime(gateway=ModelGateway(), definitions=definitions)
            registry = RuntimeRegistry()
            registry.register(restarted_runtime)
            recovery = RuntimeRecoveryService(
                queue=queue,
                audit=audit,
                runtimes=registry,
                sessions=sessions,
            )

            candidates = recovery.inspect()
            assert len(candidates) == 1
            assert candidates[0].disposition is RecoveryDisposition.AUTO_RESUME_CRASH

            recovered = await recovery.resume_safe_crash_sessions()
            assert len(recovered) == 1
            assert recovered[0].candidate.disposition is RecoveryDisposition.CHECKPOINT_UNAVAILABLE
            assert recovered[0].result is None
            assert recovered[0].error is not None
            assert "unverifiable" in recovered[0].error
            assert sessions.get(task.task_id) == persisted

            invalid_probe = await restarted_runtime.probe_resume(
                task_id=task.task_id,
                thread_id="thread-foundry-crash",
                resume_token="model-gateway-inflight-v1:wrong",
            )
            assert invalid_probe.status is RuntimeResumeProbeStatus.INVALID
            assert not invalid_probe.can_resume
        finally:
            release.set()
            result = await execution

        assert result.outcome is RuntimeOutcome.COMPLETED
        assert sessions.get(task.task_id) is None
        with store.connection() as conn:
            state = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()["state"]
        assert state == TaskState.COMPLETED.value

    asyncio.run(scenario())


def test_non_hard_cancellable_provider_does_not_commit_false_cancelled_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "nika.db")
        store.initialize()
        queue = TaskQueue(store)
        audit = AuditLog(store)
        sessions = RuntimeSessionStore(store)
        definitions = _definitions(store)

        entered = asyncio.Event()
        release = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(_BarrierLocalProvider(entered, release), default=True)
        runtime = _runtime(gateway=gateway, definitions=definitions)
        coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)

        task = queue.create(workspace_id="cancel-proof", agent_id="worker")
        queue.transition(task.task_id, TaskState.READY)
        execution = asyncio.create_task(coordinator.start(runtime, _request(task.task_id)))

        try:
            await entered.wait()
            assert RuntimeCapability.CANCELLATION not in runtime.capabilities
            assert await coordinator.cancel(
                runtime,
                task_id=task.task_id,
                thread_id="thread-foundry-crash",
            ) is False
            assert not execution.done()
            persisted = sessions.get(task.task_id)
            assert persisted is not None
            assert persisted.is_active
            with store.connection() as conn:
                state = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
                ).fetchone()["state"]
            assert state == TaskState.RUNNING.value
        finally:
            release.set()
            result = await execution

        assert result.outcome is RuntimeOutcome.COMPLETED
        assert sessions.get(task.task_id) is None
        with store.connection() as conn:
            state = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()["state"]
        assert state == TaskState.COMPLETED.value

    asyncio.run(scenario())


def test_outer_caller_cancellation_preserves_running_recovery_marker(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "nika.db")
        store.initialize()
        queue = TaskQueue(store)
        audit = AuditLog(store)
        sessions = RuntimeSessionStore(store)
        definitions = _definitions(store)

        entered = asyncio.Event()
        release = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(_BarrierLocalProvider(entered, release), default=True)
        runtime = _runtime(gateway=gateway, definitions=definitions)
        coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)

        task = queue.create(workspace_id="outer-cancel-proof", agent_id="worker")
        queue.transition(task.task_id, TaskState.READY)
        execution = asyncio.create_task(coordinator.start(runtime, _request(task.task_id)))

        await entered.wait()
        assert RuntimeCapability.CANCELLATION not in runtime.capabilities
        persisted = sessions.get(task.task_id)
        assert persisted is not None
        assert persisted.is_active

        execution.cancel()
        try:
            await execution
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("outer cancellation must propagate to the caller")

        assert sessions.get(task.task_id) == persisted
        with store.connection() as conn:
            state = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()["state"]
        assert state == TaskState.RUNNING.value

    asyncio.run(scenario())


def test_hard_cancellable_provider_is_advertised_and_can_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "nika.db")
        store.initialize()
        definitions = _definitions(store)

        entered = asyncio.Event()
        release = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(
            _BarrierLocalProvider(
                entered,
                release,
                supports_hard_cancellation=True,
            ),
            default=True,
        )
        runtime = _runtime(gateway=gateway, definitions=definitions)
        request = _request("hard-cancel-task")

        assert RuntimeCapability.CANCELLATION in runtime.capabilities
        execution = asyncio.create_task(runtime.run(request))
        await entered.wait()
        accepted = await runtime.cancel(
            task_id=request.task_id,
            thread_id=request.thread_id,
        )
        release.set()
        result = await execution

        assert accepted is True
        assert result.outcome is RuntimeOutcome.CANCELLED

    asyncio.run(scenario())


class _StickyRouteText(str):
    """Text-shaped route identity whose strip() hides its actual whitespace."""

    def strip(self, chars: str | None = None) -> str:
        del chars
        return self


class _SpoofingRuntimeTimeout(float):
    """Negative timeout that lies at the adapter's <= 0 admission check."""

    def __le__(self, other: object) -> bool:
        del other
        return False


class _BehavioralRuntimeTemperature(float):
    """Numeric-looking temperature carrier that must not be retained as authority."""


def test_runtime_route_admission_rejects_behavioral_text_carriers(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    definitions = _definitions(store)

    provider_id = _StickyRouteText(" foundry-local ")
    assert provider_id.strip() is provider_id
    with pytest.raises(TypeError, match="provider_id must be exact text"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id=provider_id,
            provider_kind=ProviderKind.LOCAL,
            model="fixture-model",
        )

    model = _StickyRouteText(" fixture-model ")
    assert model.strip() is model
    with pytest.raises(TypeError, match="model must be exact text"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model=model,
        )


def test_runtime_route_admission_rejects_behavioral_numeric_carriers(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    definitions = _definitions(store)

    timeout = _SpoofingRuntimeTimeout(-1.0)
    assert (timeout <= 0) is False
    with pytest.raises(TypeError, match="timeout_seconds"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model="fixture-model",
            timeout_seconds=timeout,
        )

    with pytest.raises(TypeError, match="temperature"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model="fixture-model",
            temperature=_BehavioralRuntimeTemperature(0.5),
        )


def test_runtime_route_admission_rejects_noncanonical_privacy_carrier(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    definitions = _definitions(store)

    with pytest.raises(TypeError, match="privacy must be PrivacyClass"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model="fixture-model",
            privacy="private",  # type: ignore[arg-type]
        )

    runtime = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="foundry-local",
        provider_kind=ProviderKind.LOCAL,
        model="fixture-model",
        timeout_seconds=3,
        privacy=PrivacyClass.PRIVATE,
        temperature=1,
    )
    request = runtime._build_model_request(_request("canonical-route"))
    assert request.provider_id == "foundry-local"
    assert request.provider_kind is ProviderKind.LOCAL
    assert request.privacy is PrivacyClass.PRIVATE
    assert request.timeout_seconds == 3
    assert request.temperature == 1.0
