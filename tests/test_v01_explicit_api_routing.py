from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import nika_core.v01_model_settings as model_settings_module
from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.runtime.contracts import AgentRuntimePort, RuntimeOutcome, RuntimeRequest
from nika_core.v01_model_settings import (
    ModelRoutePolicyPort,
    ModelSetupError,
    V01BoundModelRuntimeFactory,
    V01ModelSettings,
)


class _StaticResolver:
    def __init__(self, material: str) -> None:
        self.material = material
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return self.material


class _RecordingRoutePolicy(ModelRoutePolicyPort):
    def __init__(self, *, allow_cloud: bool) -> None:
        self.allow_cloud = allow_cloud
        self.seen: list[tuple[str, ProviderKind]] = []

    def allows(self, *, provider_id: str, provider_kind: ProviderKind) -> bool:
        self.seen.append((provider_id, provider_kind))
        return provider_kind is ProviderKind.LOCAL or self.allow_cloud


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "explicit-routing" / "nika.db")
    store.initialize()
    return store


def _definitions(store: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Complete the assigned task.",
        instructions="Return one short factual result.",
        model_profile="configured",
    )
    if repository.get("worker", 1) is None:
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _api() -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": "api-v1",
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_TEST_REFERENCE",
        "private_data_allowed": True,
        "timeout_seconds": 30,
        "revision": 0,
    }


def _local() -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 30,
        "revision": 0,
    }


def _task(settings: V01ModelSettings, store: SQLiteStore) -> str:
    payload = settings.prepare_task_payload({"command": "Run selected model route."})
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id


def _run(runtime: AgentRuntimePort, task_id: str):
    return asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id="thread:explicit-route",
                payload={
                    "agent_id": "worker",
                    "agent_version": 1,
                    "handoff": {},
                    "inbound_handoffs": [],
                },
            )
        )
    )


@pytest.mark.parametrize("missing", ("base_url", "model", "credential_ref"))
def test_api_route_missing_required_configuration_is_rejected(
    tmp_path: Path, missing: str
) -> None:
    settings = V01ModelSettings(_store(tmp_path))
    payload = _api()
    payload.pop(missing)

    result = settings.configure(payload)

    assert result.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}


def test_invalid_api_route_is_rejected_before_task_binding(tmp_path: Path) -> None:
    settings = V01ModelSettings(_store(tmp_path))

    result = settings.configure({**_api(), "base_url": "ftp://api.example.test/v1"})

    assert result.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}


def test_policy_denies_selected_cloud_before_provider_or_credential_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api()).status == "completed"
    task_id = _task(settings, store)
    policy = _RecordingRoutePolicy(allow_cloud=False)
    resolver = _StaticResolver("must-not-be-read")

    def forbidden_provider(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("denied cloud route must not construct a provider")

    monkeypatch.setattr(
        model_settings_module,
        "CredentialRefOpenAICompatibleProvider",
        forbidden_provider,
    )

    def forbidden_client_factory(**kwargs: object) -> httpx.AsyncClient:
        del kwargs
        raise AssertionError("denied cloud route must not construct a client")

    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=forbidden_client_factory,
        route_policy=policy,
    )

    with pytest.raises(ModelSetupError, match="заборонено політикою"):
        factory.for_task(task_id)

    assert policy.seen == [("configured-api", ProviderKind.CLOUD)]
    assert resolver.references == []


def test_local_selection_with_cloud_denial_never_switches_to_api(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_local()).status == "completed"
    task_id = _task(settings, store)
    policy = _RecordingRoutePolicy(allow_cloud=False)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.host in {"localhost", "127.0.0.1", "::1"}
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "message": {"role": "assistant", "content": "local-only"},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        client_factory=client_factory,
        route_policy=policy,
    ).for_task(task_id)
    result = _run(runtime, task_id)

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["provider_id"] == "ollama"
    assert result.output["provider_kind"] == "local"
    assert paths == ["/api/chat"]
    assert policy.seen == [("ollama", ProviderKind.LOCAL)]


def test_explicit_api_selection_with_policy_allow_uses_only_configured_route(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api()).status == "completed"
    task_id = _task(settings, store)
    policy = _RecordingRoutePolicy(allow_cloud=True)
    resolver = _StaticResolver("fixture-secret-material")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.host == "api.example.test"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"content": "api-only"}}],
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=client_factory,
        route_policy=policy,
    ).for_task(task_id)
    result = _run(runtime, task_id)

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["provider_id"] == "configured-api"
    assert result.output["provider_kind"] == "cloud"
    assert result.output["model"] == "api-v1"
    assert paths == ["/v1/chat/completions"]
    assert resolver.references == ["env:NIKA_TEST_REFERENCE"]
    assert policy.seen == [("configured-api", ProviderKind.CLOUD)]


def test_api_provider_failure_does_not_fall_back_to_local(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api()).status == "completed"
    task_id = _task(settings, store)
    policy = _RecordingRoutePolicy(allow_cloud=True)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(503, json={"error": "fixture unavailable"})

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=_StaticResolver("fixture-secret-material"),
        client_factory=client_factory,
        route_policy=policy,
    ).for_task(task_id)
    result = _run(runtime, task_id)

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.output["provider_id"] == "configured-api"
    assert paths == ["/v1/chat/completions"]
