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
