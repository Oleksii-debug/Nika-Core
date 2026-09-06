from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
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
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            name=agent_id,
            goal="Complete the assigned task.",
            instructions="Return concise evidence and do not invent tool use.",
            model_profile="configured",
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return store, repository


def _runtime_request(
    *, task_id: str = "task-1", thread_id: str = "thread-1"
) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id=thread_id,
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "summarize fixture"},
        },
    )


def _run_three_agent(
    *,
    sqlite: SQLiteStore,
    definitions: AgentDefinitionRepository,
    runtime: ModelGatewayAgentRuntime,
    team_id: str,
) -> tuple[object, ...]:
    team_store = MultiAgentStore(sqlite)
    team_store.create_team(
        team_id=team_id,
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=team_store,
        definitions=definitions,
    )
    return asyncio.run(
        supervisor.fan_out(
            team_id=team_id,
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


def test_three_agent_local_route_uses_same_model_gateway_adapter(tmp_path: Path) -> None:
    sqlite, definitions = _definitions(tmp_path)
    requests_seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content.decode("utf-8"))
        requests_seen.append(payload)
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["model"] == "qwen3:8b"
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "local-result"},
                "prompt_eval_count": 11,
                "eval_count": 3,
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(default_model="qwen3:8b", client_factory=client_factory),
        default=True,
    )
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        model="qwen3:8b",
        timeout_seconds=2,
    )

    executions = _run_three_agent(
        sqlite=sqlite,
        definitions=definitions,
        runtime=runtime,
        team_id="team-local",
    )

    assert len(requests_seen) == 2
    assert RuntimeCapability.LOCAL_MODELS in runtime.capabilities
    for execution in executions:
        assert execution.result is not None
        assert execution.result.outcome is RuntimeOutcome.COMPLETED
        assert execution.result.output["provider_id"] == "ollama"
        assert execution.result.output["provider_kind"] == "local"
        assert execution.result.output["model"] == "qwen3:8b"
        assert execution.result.output["text"] == "local-result"


class _StaticCredentialResolver:
    def __init__(self, material: str) -> None:
        self.material = material
        self.refs: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.refs.append(credential_ref)
        return self.material


def test_three_agent_configured_api_route_uses_same_model_gateway_adapter(
    tmp_path: Path,
) -> None:
    sqlite, definitions = _definitions(tmp_path)
    secret = "nika-api-canary-do-not-log"
    resolver = _StaticCredentialResolver(secret)
    requests_seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {secret}"
        payload = json.loads(request.content.decode("utf-8"))
        requests_seen.append(payload)
        assert payload["model"] == "fixture-api-model"
        return httpx.Response(
            200,
            json={
                "model": "fixture-api-model",
                "choices": [{"message": {"role": "assistant", "content": "api-result"}}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="configured-api",
            base_url="https://api.fixture.invalid/v1",
            default_model="fixture-api-model",
            credential_ref="env:NIKA_FIXTURE_API_KEY",
            supports_private_data=True,
        ),
        credential_resolver=resolver,
        client_factory=client_factory,
    )
    gateway = ModelGateway()
    gateway.register(provider, default=True)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="configured-api",
        provider_kind=ProviderKind.CLOUD,
        model="fixture-api-model",
        timeout_seconds=2,
    )

    executions = _run_three_agent(
        sqlite=sqlite,
        definitions=definitions,
        runtime=runtime,
        team_id="team-api",
    )

    assert len(requests_seen) == 2
    assert resolver.refs == ["env:NIKA_FIXTURE_API_KEY", "env:NIKA_FIXTURE_API_KEY"]
    assert RuntimeCapability.LOCAL_MODELS not in runtime.capabilities
    for execution in executions:
        assert execution.result is not None
        assert execution.result.outcome is RuntimeOutcome.COMPLETED
        assert execution.result.output["provider_id"] == "configured-api"
        assert execution.result.output["provider_kind"] == "cloud"
        assert execution.result.output["model"] == "fixture-api-model"
        assert execution.result.output["text"] == "api-result"
        assert secret not in repr(dict(execution.result.output))


class _ExplodingCredentialResolver:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def resolve(self, credential_ref: str) -> str:
        raise RuntimeError(f"credential failure contains {self.secret} for {credential_ref}")


def test_configured_api_credential_failure_is_secret_safe_and_no_effect(
    tmp_path: Path,
) -> None:
    _, definitions = _definitions(tmp_path)
    secret = "nika-secret-canary-9341"
    provider = CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="configured-api",
            base_url="https://api.fixture.invalid/v1",
            default_model="fixture-api-model",
            credential_ref="env:NIKA_FIXTURE_API_KEY",
            supports_private_data=True,
        ),
        credential_resolver=_ExplodingCredentialResolver(secret),
    )
    gateway = ModelGateway()
    gateway.register(provider)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="configured-api",
        provider_kind=ProviderKind.CLOUD,
        model="fixture-api-model",
    )

    result = asyncio.run(runtime.run(_runtime_request()))

    rendered = repr(result)
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.output["model_error_code"] == "authentication"
    assert result.output["provider_id"] == "configured-api"
    assert result.output["recoverable"] is False
    assert result.output["provider_retryable"] is False
    assert result.output["failure_effect"] == "no_effect"
    assert secret not in rendered
    assert "NIKA_FIXTURE_API_KEY" not in rendered


class _WrongKindProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="route",
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="wrong-kind",
            provider_id="route",
            provider_kind=ProviderKind.CLOUD,
            model=request.model or "fixture",
        )


def test_runtime_fails_closed_on_provider_kind_substitution(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    gateway = ModelGateway()
    gateway.register(_WrongKindProvider())
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="route",
        provider_kind=ProviderKind.LOCAL,
        model="fixture",
    )

    result = asyncio.run(runtime.run(_runtime_request()))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.output["provider_id"] == "route"
    assert result.output["provider_kind"] == "cloud"
    assert result.error == "The model response did not match the configured provider route."


def test_adapter_does_not_claim_durable_resume(tmp_path: Path) -> None:
    _, definitions = _definitions(tmp_path)
    runtime = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
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
