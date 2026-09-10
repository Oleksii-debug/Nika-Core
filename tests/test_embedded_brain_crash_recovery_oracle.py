from __future__ import annotations

import asyncio
from pathlib import Path

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
    ProviderCapabilities,
    ProviderKind,
)
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


def test_active_embedded_inference_has_durable_crash_marker_before_provider_returns(
    tmp_path: Path,
) -> None:
    """RED oracle: a crash-left model request must be visible to startup recovery.

    No sleep or timing guess is used.  The provider barrier marks the exact window
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
