from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceMode
from nika_core.intelligence.provenance import IntelligenceProvenance
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings


class _StaticResolver:
    def __init__(self, material: str) -> None:
        self.material = material
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return self.material


class _SwitchTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.local_started = asyncio.Event()
        self.local_release = asyncio.Event()
        self.block_first_local = True
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = str(payload["model"])
        self.calls.append((request.url.path, model))
        if request.url.path == "/api/chat":
            if self.block_first_local:
                self.block_first_local = False
                self.local_started.set()
                await self.local_release.wait()
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": model,
                    "message": {"role": "assistant", "content": "local-A"},
                },
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": model,
                    "choices": [{"message": {"content": "api-B"}}],
                },
            )
        raise AssertionError(f"unexpected model endpoint: {request.url.path}")


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
        goal="Return bounded evidence.",
        instructions="Return one short result.",
        model_profile="configured",
    )
    if repository.get("worker", 1) is None:
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _local(*, revision: int = 0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "local-A-model",
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 30,
        "revision": revision,
    }


def _api(*, revision: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": "api-B-model",
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_SWITCH_TEST_REFERENCE",
        "private_data_allowed": True,
        "timeout_seconds": 30,
        "revision": revision,
    }


def _create_task(settings: V01ModelSettings, store: SQLiteStore, command: str) -> str:
    payload = settings.prepare_task_payload({"command": command})
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id


def _request(task_id: str, thread_id: str) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id=thread_id,
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": f"complete {task_id}"},
        },
    )


def _assert_provenance(
    result: object,
    *,
    mode: IntelligenceMode,
    provider_id: str,
    model: str,
    correlation: str,
) -> None:
    assert getattr(result, "outcome") is RuntimeOutcome.COMPLETED
    output = getattr(result, "output")
    assert output["provider_id"] == provider_id
    assert output["model"] == model
    provenance = IntelligenceProvenance.from_payload(output["intelligence_provenance"])
    assert provenance.intelligence_mode is mode
    assert provenance.provider_id == provider_id
    assert provenance.model_fingerprint == model_identity_fingerprint(model)
    assert provenance.request_correlation_id == correlation


def test_inflight_local_request_keeps_route_after_api_switch_and_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_local()).status == "completed"
    task_a = _create_task(settings, store, "request A")

    resolver = _StaticResolver("switch-test-secret-material")

    async def scenario() -> tuple[object, object]:
        transport = _SwitchTransport()

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, **kwargs)

        factory = V01BoundModelRuntimeFactory(
            store=store,
            definitions=definitions,
            settings=settings,
            credential_resolver=resolver,
            client_factory=client_factory,
        )
        runtime_a = factory.for_task(task_a)
        pending_a = asyncio.create_task(runtime_a.run(_request(task_a, "thread-a")))
        await asyncio.wait_for(transport.local_started.wait(), timeout=1.0)

        # A is already inside the selected local provider. Change the durable
        # current setting while it is in flight, then accept and execute B.
        assert settings.configure(_api()).status == "completed"
        task_b = _create_task(settings, store, "request B")
        runtime_b = factory.for_task(task_b)
        result_b = await runtime_b.run(_request(task_b, "thread-b"))

        transport.local_release.set()
        result_a = await pending_a

        _assert_provenance(
            result_a,
            mode=IntelligenceMode.EXTERNAL_LOCAL,
            provider_id="ollama",
            model="local-A-model",
            correlation=f"{task_a}:thread-a",
        )
        _assert_provenance(
            result_b,
            mode=IntelligenceMode.EXTERNAL_API,
            provider_id="configured-api",
            model="api-B-model",
            correlation=f"{task_b}:thread-b",
        )
        assert transport.calls[:2] == [
            ("/api/chat", "local-A-model"),
            ("/v1/chat/completions", "api-B-model"),
        ]

        # Simulate process restart: reconstruct settings, definitions and the
        # runtime factory from the same SQLite file. Historical task bindings
        # must remain authoritative rather than inheriting the current setting.
        restarted_store = SQLiteStore(store.path)
        restarted_settings = V01ModelSettings(restarted_store)
        restarted_definitions = AgentDefinitionRepository(restarted_store)
        restarted_factory = V01BoundModelRuntimeFactory(
            store=restarted_store,
            definitions=restarted_definitions,
            settings=restarted_settings,
            credential_resolver=resolver,
            client_factory=client_factory,
        )

        restored_a = restarted_settings.for_task(task_a)
        restored_b = restarted_settings.for_task(task_b)
        assert restored_a.provider_id == "ollama"
        assert restored_a.model == "local-A-model"
        assert restored_b.provider_id == "configured-api"
        assert restored_b.model == "api-B-model"

        replay_a = await restarted_factory.for_task(task_a).run(
            _request(task_a, "thread-a-after-restart")
        )
        replay_b = await restarted_factory.for_task(task_b).run(
            _request(task_b, "thread-b-after-restart")
        )
        _assert_provenance(
            replay_a,
            mode=IntelligenceMode.EXTERNAL_LOCAL,
            provider_id="ollama",
            model="local-A-model",
            correlation=f"{task_a}:thread-a-after-restart",
        )
        _assert_provenance(
            replay_b,
            mode=IntelligenceMode.EXTERNAL_API,
            provider_id="configured-api",
            model="api-B-model",
            correlation=f"{task_b}:thread-b-after-restart",
        )

        with restarted_store.connection() as conn:
            durable = [
                *(str(row[0]) for row in conn.execute("SELECT payload_json FROM tasks")),
                *(str(row[0]) for row in conn.execute("SELECT selection_json FROM v01_model_selections")),
                *(
                    str(row[0])
                    for row in conn.execute("SELECT selection_json FROM v01_task_model_bindings")
                ),
            ]
        assert "switch-test-secret-material" not in repr(durable)
        return result_a, result_b

    result_a, result_b = asyncio.run(scenario())
    assert result_a.output["provider_id"] == "ollama"
    assert result_b.output["provider_id"] == "configured-api"
