from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.multi_agent.contracts import ChildRequest, TeamQuota
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeRequest,
)


def _definitions(tmp_path: Path) -> tuple[SQLiteStore, AgentDefinitionRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"local"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            name=agent_id,
            goal="Complete the assigned task.",
            instructions="Return concise evidence and do not invent tool use.",
            model_profile="local",
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return store, repository


def _runtime_request(*, task_id: str = "task-1", thread_id: str = "thread-1") -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id=thread_id,
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "summarize fixture"},
        },
    )


def test_three_agent_supervisor_uses_native_ollama_through_model_gateway(tmp_path: Path) -> None:
    sqlite, definitions = _definitions(tmp_path)
    requests_seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content.decode("utf-8"))
        requests_seen.append(payload)
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["model"] == "qwen3:8b"
        assert len(payload["messages"]) == 2
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "fixture-result"},
                "prompt_eval_count": 11,
                "eval_count": 3,
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(
            default_model="qwen3:8b",
            client_factory=client_factory,
        ),
        default=True,
    )
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="ollama",
        model="qwen3:8b",
        timeout_seconds=2,
    )
    team_store = MultiAgentStore(sqlite)
    team_store.create_team(
        team_id="team-local",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(max_depth=2, max_children_per_parent=2, max_total_agents=3, max_parallel=2),
    )
    supervisor = MultiAgentSupervisor(runtime=runtime, store=team_store, definitions=definitions)

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-local",
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="worker-1",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-worker-1",
                    payload={"target": "one"},
                ),
                ChildRequest(
                    member_id="worker-2",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-worker-2",
                    payload={"target": "two"},
                ),
            ),
        )
    )

    assert len(requests_seen) == 2
    assert all(item.result is not None for item in executions)
    for execution in executions:
        assert execution.result is not None
        assert execution.result.outcome is RuntimeOutcome.COMPLETED
        assert execution.result.output["provider_id"] == "ollama"
        assert execution.result.output["provider_kind"] == "local"
        assert execution.result.output["model"] == "qwen3:8b"
        assert execution.result.output["text"] == "fixture-result"


def test_unavailable_local_provider_returns_recoverable_accessible_failure(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    runtime = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="ollama",
        model="qwen3:8b",
    )

    result = asyncio.run(runtime.run(_runtime_request()))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.TRANSIENT
    assert result.output == {
        "model_error_code": "unavailable",
        "provider_id": "ollama",
        "recoverable": True,
        "provider_retryable": False,
    }
    assert result.error == (
        "The local model provider is unavailable. Start or configure it, then retry."
    )


class _SlowLocalProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._capabilities = ProviderCapabilities(
            provider_id="ollama",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ModelResponse(
            request_id=request.request_id,
            text="late",
            provider_id="ollama",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "qwen3:8b",
        )


def test_timeout_maps_to_runtime_timeout_without_fallback(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    gateway = ModelGateway()
    gateway.register(_SlowLocalProvider(), default=True)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        model="qwen3:8b",
        timeout_seconds=0.01,
    )

    result = asyncio.run(runtime.run(_runtime_request()))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.TIMEOUT
    assert result.output["model_error_code"] == "timeout"
    assert result.output["provider_id"] == "ollama"
    assert result.output["recoverable"] is True
    assert result.output["provider_retryable"] is False


def test_cancel_stops_active_gateway_coroutine_without_hard_cancel_claim(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    provider = _SlowLocalProvider()
    gateway = ModelGateway()
    gateway.register(provider, default=True)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        model="qwen3:8b",
        timeout_seconds=30,
    )

    async def scenario() -> tuple[bool, RuntimeOutcome]:
        running = asyncio.create_task(runtime.run(_runtime_request()))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        cancelled = await runtime.cancel(task_id="task-1", thread_id="thread-1")
        result = await asyncio.wait_for(running, timeout=1)
        assert provider.cancelled.is_set()
        return cancelled, result.outcome

    cancelled, outcome = asyncio.run(scenario())

    assert cancelled is True
    assert outcome is RuntimeOutcome.CANCELLED
    assert provider.capabilities.supports_hard_cancellation is False


def test_adapter_does_not_claim_durable_resume(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    runtime = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
    )

    assert RuntimeCapability.DURABLE_RESUME not in runtime.capabilities
    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id="task-1",
                thread_id="thread-1",
                resume_token="opaque",
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INVALID_RESUME
    assert result.output["recoverable"] is True
