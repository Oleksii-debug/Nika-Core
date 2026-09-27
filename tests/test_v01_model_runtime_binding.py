from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceModePolicy
from nika_core.kernel.task_queue import TaskQueue
from nika_core.multi_agent.contracts import AgentHandoff, ChildRequest, HandoffKind, TeamQuota
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import ChildExecution, MultiAgentSupervisor
from nika_core.runtime.contracts import AgentRuntimePort, RuntimeOutcome
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings


class _StaticResolver:
    def __init__(self, material: str) -> None:
        self.material = material
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return self.material


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Профіль користувача" / "ніка.db")
    store.initialize()
    return store


def _definitions(store: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            name=agent_id,
            goal="Complete the assigned task.",
            instructions="Return one short factual result.",
            model_profile="configured",
        )
        if repository.get(agent_id, 1) is None:
            repository.save_draft(compiler.compile(definition))
            repository.activate(definition)
    return repository


def _api(
    *,
    revision: int = 0,
    model: str = "api-v1",
    private_data_allowed: bool = True,
    timeout_seconds: float = 30,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": model,
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_TEST_REFERENCE",
        "private_data_allowed": private_data_allowed,
        "timeout_seconds": timeout_seconds,
        "revision": revision,
    }


def _api_policy() -> IntelligenceModePolicy:
    return IntelligenceModePolicy(
        external_api_enabled=True,
        external_provider_id="configured-api",
    )


def _local(*, revision: int = 0, model: str = "qwen3:8b") -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": model,
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
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


def _run_three_members(
    *,
    store: SQLiteStore,
    definitions: AgentDefinitionRepository,
    runtime: AgentRuntimePort,
    team_id: str,
) -> tuple[ChildExecution, ...]:
    team_store = MultiAgentStore(store)
    team_store.create_team(
        team_id=team_id,
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id=f"thread:{team_id}:root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
        root_task_handoff=AgentHandoff(
            team_id=team_id,
            sender_id="root",
            recipient_id="root",
            kind=HandoffKind.TASK,
            payload={"work": "Check both worker results."},
            handoff_id=f"task:{team_id}:root",
            correlation_id=f"team:{team_id}:root",
        ),
    )
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=team_store,
        definitions=definitions,
    )

    async def scenario() -> tuple[ChildExecution, ...]:
        workers = await supervisor.fan_out(
            team_id=team_id,
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="worker-a",
                    agent_id="worker",
                    agent_version=1,
                    thread_id=f"thread:{team_id}:worker-a",
                    payload={"work": "Return worker A result."},
                ),
                ChildRequest(
                    member_id="worker-b",
                    agent_id="worker",
                    agent_version=1,
                    thread_id=f"thread:{team_id}:worker-b",
                    payload={"work": "Return worker B result."},
                ),
            ),
        )
        root = await supervisor.run_root_member(team_id=team_id, member_id="root")
        supervisor.finalize_team(team_id)
        return (*workers, root)

    return asyncio.run(scenario())


def test_bound_api_route_drives_all_three_members_and_survives_settings_change_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api()).status == "completed"
    task_id = _create_task(settings, store, "Run API team.")

    observed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        if request.url.path == "/v1/chat/completions":
            observed.append(("api", str(payload["model"])))
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "choices": [{"message": {"content": "api result"}}],
                },
            )
        if request.url.path == "/api/chat":
            observed.append(("local", str(payload["model"])))
            assert payload["stream"] is False
            assert payload["think"] is False
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "message": {"role": "assistant", "content": "local result"},
                },
            )
        raise AssertionError(f"unexpected model endpoint: {request.url.path}")

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    resolver = _StaticResolver("example-material")
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=client_factory,
        intelligence_policy=_api_policy(),
    )
    executions = _run_three_members(
        store=store,
        definitions=definitions,
        runtime=factory.for_task(task_id),
        team_id="team-api-first",
    )

    assert len(executions) == 3
    for execution in executions:
        result = execution.result
        assert result is not None
        assert result.outcome is RuntimeOutcome.COMPLETED
        assert result.output["provider_id"] == "configured-api"
        assert result.output["provider_kind"] == "cloud"
        assert result.output["model"] == "api-v1"
    assert observed == [("api", "api-v1")] * 3

    assert settings.configure(_local(revision=1)).status == "completed"
    restarted_store = SQLiteStore(store.path)
    restarted_settings = V01ModelSettings(restarted_store)
    restarted_definitions = AgentDefinitionRepository(restarted_store)
    restarted_factory = V01BoundModelRuntimeFactory(
        store=restarted_store,
        definitions=restarted_definitions,
        settings=restarted_settings,
        credential_resolver=resolver,
        client_factory=client_factory,
        intelligence_policy=_api_policy(),
    )
    restarted = _run_three_members(
        store=restarted_store,
        definitions=restarted_definitions,
        runtime=restarted_factory.for_task(task_id),
        team_id="team-api-after-restart",
    )
    assert len(restarted) == 3
    assert observed == [("api", "api-v1")] * 6

    new_task_id = _create_task(restarted_settings, restarted_store, "Run local team.")
    local = _run_three_members(
        store=restarted_store,
        definitions=restarted_definitions,
        runtime=restarted_factory.for_task(new_task_id),
        team_id="team-local-new-task",
    )
    assert len(local) == 3
    assert observed[-3:] == [("local", "qwen3:8b")] * 3
    assert all(
        execution.result is not None
        and execution.result.output["provider_id"] == "ollama"
        and execution.result.output["model"] == "qwen3:8b"
        for execution in local
    )

    with restarted_store.connection() as conn:
        durable = [
            *(str(row[0]) for row in conn.execute("SELECT payload_json FROM tasks")),
            *(str(row[0]) for row in conn.execute("SELECT payload_json FROM audit_events")),
            *(str(row[0]) for row in conn.execute("SELECT selection_json FROM v01_model_settings")),
            *(str(row[0]) for row in conn.execute("SELECT selection_json FROM v01_model_selections")),
            *(
                str(row[0])
                for row in conn.execute("SELECT selection_json FROM v01_task_model_bindings")
            ),
        ]
    assert "example-material" not in repr(durable)
    assert resolver.references == ["env:NIKA_TEST_REFERENCE"] * 6


def test_bound_local_route_only_calls_ollama_chat_and_never_acquires_model(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_local()).status == "completed"
    task_id = _create_task(settings, store, "Run local team.")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "qwen3:8b"
        assert payload["stream"] is False
        assert payload["think"] is False
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "local result"},
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
    ).for_task(task_id)

    executions = _run_three_members(
        store=store,
        definitions=definitions,
        runtime=runtime,
        team_id="team-local-only",
    )

    assert len(executions) == 3
    assert paths == ["/api/chat"] * 3


def test_configured_api_failure_never_switches_to_local_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api()).status == "completed"
    task_id = _create_task(settings, store, "Run selected API route.")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503, json={"error": "fixture unavailable"})

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=_StaticResolver("example-material"),
        client_factory=client_factory,
        intelligence_policy=_api_policy(),
    ).for_task(task_id)

    executions = _run_three_members(
        store=store,
        definitions=definitions,
        runtime=runtime,
        team_id="team-api-unavailable",
    )

    assert paths == ["/v1/chat/completions"] * 3
    for execution in executions:
        result = execution.result
        if result is not None:
            assert result.outcome is RuntimeOutcome.FAILED
            assert result.output.get("provider_id") == "configured-api"


def test_api_private_data_permission_fails_before_credential_resolution_or_transport(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert settings.configure(_api(private_data_allowed=False)).status == "completed"
    task_id = _create_task(settings, store, "Run selected API route.")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        raise AssertionError("private route must fail before transport")

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    resolver = _StaticResolver("example-material")
    runtime = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=client_factory,
        intelligence_policy=_api_policy(),
    ).for_task(task_id)

    executions = _run_three_members(
        store=store,
        definitions=definitions,
        runtime=runtime,
        team_id="team-api-private-blocked",
    )

    assert paths == []
    assert resolver.references == []
    for execution in executions:
        assert execution.result is not None
        assert execution.result.outcome is RuntimeOutcome.FAILED
        assert execution.result.output["model_error_code"] == "invalid_request"
        assert execution.result.output["recoverable"] is False


class _SlowApiTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.08)
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "slow-model",
                "choices": [{"message": {"content": "late"}}],
            },
        )


def test_bound_supervisor_uses_frozen_model_timeout_instead_of_default(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    definitions = _definitions(store)
    assert (
        settings.configure(_api(model="slow-model", timeout_seconds=0.01)).status
        == "completed"
    )
    task_id = _create_task(settings, store, "Run bounded API task.")

    team_id = "team-bound-timeout"
    team_store = MultiAgentStore(store)
    team_store.create_team(
        team_id=team_id,
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id=f"thread:{team_id}:root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
        root_task_handoff=AgentHandoff(
            team_id=team_id,
            sender_id="root",
            recipient_id="root",
            kind=HandoffKind.TASK,
            payload={"work": "Return one result."},
            handoff_id=f"task:{team_id}:root",
            correlation_id=f"team:{team_id}:root",
        ),
    )

    transport = _SlowApiTransport()

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=_StaticResolver("example-material"),
        client_factory=client_factory,
        intelligence_policy=_api_policy(),
    )
    supervisor = factory.supervisor_for_task(task_id, store=team_store)

    execution = asyncio.run(
        supervisor.run_root_member(team_id=team_id, member_id="root")
    )

    assert execution.result is not None
    assert execution.result.outcome is RuntimeOutcome.FAILED
    assert execution.result.output["model_error_code"] == "timeout"
    assert execution.result.output["provider_id"] == "configured-api"
