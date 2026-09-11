from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceModePolicy
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.v01_model_settings import (
    ModelSetupError,
    V01BoundModelRuntimeFactory,
    V01ModelSettings,
)


class _StaticResolver:
    def __init__(self, material: str = "fixture-secret") -> None:
        self.material = material
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return self.material


class _FakeFoundryModel:
    def __init__(self, *, cached: bool = True) -> None:
        self.id = "embedded-model-id"
        self.alias = "embedded-small"
        self.is_cached = cached
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat,completion"
        self.supports_tool_calling = False
        self.settings = SimpleNamespace(temperature=None)
        self.downloaded = False

    def download(self, **_: object) -> None:
        self.downloaded = True
        self.is_cached = True

    def get_path(self) -> str:
        return "C:/Nika Test Models/embedded-small"

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def get_chat_client(self) -> object:
        class Client:
            settings = self.settings

            @staticmethod
            def complete_chat(messages: list[dict[str, str]]) -> object:
                assert messages[-1]["role"] == "user"
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="embedded result")
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=3,
                        completion_tokens=2,
                        total_tokens=5,
                    ),
                )

        return Client()


class _FakeFoundryCatalog:
    def __init__(self, model: _FakeFoundryModel) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> _FakeFoundryModel:
        self.requested_aliases.append(alias)
        self.model.alias = alias
        return self.model

    def get_loaded_models(self) -> list[_FakeFoundryModel]:
        return [self.model] if self.model.is_loaded else []


class _FakeFoundryManager:
    def __init__(self, model: _FakeFoundryModel) -> None:
        self.catalog = _FakeFoundryCatalog(model)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
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
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


def _configure(
    settings: V01ModelSettings,
    *,
    route_kind: str,
    provider_id: str | None,
    model: str | None,
    base_url: str | None,
    credential_ref: str | None = None,
    private_data_allowed: bool = False,
    revision: int = 0,
) -> None:
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": route_kind,
            "provider_id": provider_id,
            "model": model,
            "base_url": base_url,
            "credential_ref": credential_ref,
            "private_data_allowed": private_data_allowed,
            "timeout_seconds": 30,
            "revision": revision,
        }
    )
    assert result.status == "completed", result


def _task(settings: V01ModelSettings, store: SQLiteStore, label: str) -> str:
    payload = settings.prepare_task_payload({"command": label})
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id


def _request(task_id: str) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id=f"thread:{task_id}",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "Return the fixture result."},
        },
    )


def test_explicit_deterministic_selection_is_durable_and_never_builds_gateway(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure(
        settings,
        route_kind="deterministic",
        provider_id=None,
        model=None,
        base_url=None,
    )
    assert settings.snapshot() == {
        "status": "ready",
        "revision": 1,
        "intelligence_mode": "no_llm",
        "route_kind": "deterministic",
        "provider_id": None,
        "provider_kind": None,
        "model": None,
        "base_url": None,
        "timeout_seconds": 30.0,
        "private_data_allowed": True,
        "credential_configured": False,
    }
    task_id = _task(settings, store, "deterministic")
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=_definitions(store),
        settings=settings,
    )
    assert factory.for_task(task_id) is None

    restarted_store = SQLiteStore(store.path)
    restarted_settings = V01ModelSettings(restarted_store)
    restarted_factory = V01BoundModelRuntimeFactory(
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_store),
        settings=restarted_settings,
    )
    assert restarted_settings.for_task(task_id).intelligence_mode == "no_llm"
    assert restarted_factory.for_task(task_id) is None


def test_embedded_selection_reaches_foundry_gateway_without_download(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure(
        settings,
        route_kind="foundry_local",
        provider_id="foundry-local",
        model="embedded-small",
        base_url=None,
    )
    task_id = _task(settings, store, "embedded")
    foundry_model = _FakeFoundryModel(cached=True)
    manager = _FakeFoundryManager(foundry_model)
    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=_definitions(store),
        settings=settings,
        foundry_manager_factory=lambda: manager,
    ).for_task(task_id)
    assert runtime is not None

    result = asyncio.run(runtime.run(_request(task_id)))

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["provider_id"] == "foundry-local"
    assert result.output["provider_kind"] == "local"
    assert result.output["model"] == "embedded-small"
    assert result.output["text"] == "embedded result"
    assert manager.catalog.requested_aliases == ["embedded-small"]
    assert foundry_model.downloaded is False


def test_local_external_and_configured_api_reach_only_selected_gateway_routes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, str(payload["model"])))
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "message": {"role": "assistant", "content": "ollama result"},
                },
            )
        if request.url.path == "/v1/chat/completions":
            assert request.headers["authorization"] == "Bearer fixture-secret"
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "choices": [{"message": {"content": "api result"}}],
                },
            )
        raise AssertionError(f"unexpected route: {request.url.path}")

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    _configure(
        settings,
        route_kind="ollama",
        provider_id="ollama",
        model="qwen3:8b",
        base_url="http://localhost:11434",
    )
    local_task = _task(settings, store, "local external")
    local_runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        client_factory=client_factory,
    ).for_task(local_task)
    assert local_runtime is not None
    local_result = asyncio.run(local_runtime.run(_request(local_task)))
    assert local_result.outcome is RuntimeOutcome.COMPLETED
    assert local_result.output["provider_id"] == "ollama"
    assert local_result.output["model"] == "qwen3:8b"

    _configure(
        settings,
        route_kind="openai_compatible",
        provider_id="configured-api",
        model="api-v1",
        base_url="https://api.example.test/v1",
        credential_ref="env:NIKA_TEST_REFERENCE",
        private_data_allowed=True,
        revision=1,
    )
    api_task = _task(settings, store, "configured API")
    resolver = _StaticResolver()
    api_runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=client_factory,
        intelligence_policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="configured-api",
        ),
    ).for_task(api_task)
    assert api_runtime is not None
    api_result = asyncio.run(api_runtime.run(_request(api_task)))
    assert api_result.outcome is RuntimeOutcome.COMPLETED
    assert api_result.output["provider_id"] == "configured-api"
    assert api_result.output["provider_kind"] == "cloud"
    assert api_result.output["model"] == "api-v1"

    assert calls == [
        ("/api/chat", "qwen3:8b"),
        ("/v1/chat/completions", "api-v1"),
    ]
    assert resolver.references == ["env:NIKA_TEST_REFERENCE"]


def test_missing_or_invalid_model_selection_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)

    with pytest.raises(ModelSetupError, match="виберіть режим"):
        settings.prepare_task_payload({"command": "must not silently fall back"})

    embedded_missing_model = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "foundry_local",
            "provider_id": "foundry-local",
            "model": None,
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert embedded_missing_model.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}

    local_blank_model = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": " ",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert local_blank_model.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}


def test_uncached_embedded_model_fails_without_downloading_or_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    _configure(
        settings,
        route_kind="foundry_local",
        provider_id="foundry-local",
        model="embedded-small",
        base_url=None,
    )
    task_id = _task(settings, store, "embedded unavailable")
    foundry_model = _FakeFoundryModel(cached=False)
    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=_definitions(store),
        settings=settings,
        foundry_manager_factory=lambda: _FakeFoundryManager(foundry_model),
    ).for_task(task_id)
    assert runtime is not None

    result = asyncio.run(runtime.run(_request(task_id)))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.output["provider_id"] == "foundry-local"
    assert foundry_model.downloaded is False
