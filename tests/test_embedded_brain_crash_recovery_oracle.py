from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.session_store import RuntimeSessionStore


class _BarrierLocalProvider:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
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


class _Catalog:
    def __init__(self, model: object) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> object:
        self.requested_aliases.append(alias)
        return self.model


class _Manager:
    def __init__(self, model: object) -> None:
        self.catalog = _Catalog(model)


class _NativeCrashModel:
    id = "fixture-model-id"
    alias = "fixture-model"
    is_cached = True
    is_loaded = True

    def get_chat_client(self) -> object:
        class Client:
            settings = SimpleNamespace(temperature=None)

            @staticmethod
            def complete_chat(messages: list[dict[str, str]]) -> object:
                del messages
                raise RuntimeError("simulated native provider process crash")

        return Client()


class _InterruptedLoadModel:
    id = "fixture-model-id"
    alias = "fixture-model"
    is_cached = True
    is_loaded = False

    def __init__(self) -> None:
        self.load_calls = 0
        self.unload_calls = 0

    def load(self) -> None:
        self.load_calls += 1
        raise RuntimeError("simulated interrupted model load")

    def unload(self) -> None:
        self.unload_calls += 1

    @staticmethod
    def get_chat_client() -> object:
        raise AssertionError("chat client must not be created after failed load")


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


def _runtime_request(task_id: str) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id="thread-foundry-crash",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "deterministic crash-window fixture"},
        },
    )


def _model_request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="deterministic fixture"),),
        model="fixture-model",
        provider_id="foundry-local",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=1.0,
    )


def test_foundry_sdk_import_failure_is_unavailable_not_success() -> None:
    def missing_sdk() -> object:
        raise ModuleNotFoundError("simulated missing foundry_local_sdk")

    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=missing_sdk,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_model_request("sdk-missing")))

    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is False


def test_foundry_native_provider_crash_is_provider_error_not_success() -> None:
    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: _Manager(_NativeCrashModel()),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_model_request("provider-crash")))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is False


def test_interrupted_foundry_model_load_never_reaches_inference_or_owned_unload() -> None:
    model = _InterruptedLoadModel()
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_model_request("load-interrupted")))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert manager.catalog.requested_aliases == ["fixture-model"]
    assert model.load_calls == 1

    provider.close()
    assert model.unload_calls == 0


def test_active_embedded_inference_has_durable_crash_marker_before_provider_returns(
    tmp_path: Path,
) -> None:
    """RED oracle: a crash-left model request must be visible to startup recovery.

    No sleep or timing guess is used. The provider barrier marks the exact window
    after ModelGateway inference has started but before it can return a response.
    If the Nika/Windows process terminates at that point, a durable runtime-session
    marker is required so canonical startup recovery can classify the request as
    non-resumable/unverifiable instead of silently leaving an orphan RUNNING task.
    """

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
        runtime = ModelGatewayAgentRuntime(
            gateway=gateway,
            definitions=definitions,
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model="fixture-model",
        )
        coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)

        task = queue.create(workspace_id="proof", agent_id="worker")
        queue.transition(task.task_id, TaskState.READY)
        execution = asyncio.create_task(coordinator.start(runtime, _runtime_request(task.task_id)))

        try:
            await entered.wait()
            with store.connection() as conn:
                state = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
                ).fetchone()["state"]
            assert state == TaskState.RUNNING.value

            persisted = sessions.get(task.task_id)
            assert persisted is not None, (
                "crash-left Embedded Brain inference has no durable RuntimeSessionStore marker; "
                "RuntimeRecoveryService cannot classify this RUNNING request after restart"
            )
            assert persisted.runtime_id == runtime.runtime_id
            assert persisted.thread_id == "thread-foundry-crash"
        finally:
            release.set()
            result = await execution
            assert result.outcome is RuntimeOutcome.COMPLETED

    asyncio.run(scenario())
