from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.security.model_cloud_authority import (
    StandingPermissionCloudEffectAuthorizer,
    StandingPermissionExecutionAuthority,
)
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk
from nika_core.v01_model_settings import (
    V01BoundModelRuntimeFactory,
    V01ModelSettings,
)


class _CountingCredentialResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return "fixture-secret-material"


class _CountingTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.tasks: list[asyncio.Task[object] | None] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.tasks.append(asyncio.current_task())
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": "qwen3:8b",
                    "message": {"role": "assistant", "content": "authorized local result"},
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "api-model",
                "choices": [{"message": {"content": "authorized cloud result"}}],
            },
        )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.sqlite3")
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
            instructions="Return a short factual result.",
            model_profile="configured",
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _settings(store: SQLiteStore) -> V01ModelSettings:
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "openai_compatible",
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:NIKA_PACKAGED_CLOUD_KEY",
            "private_data_allowed": True,
            "timeout_seconds": 10,
            "revision": 0,
        }
    )
    assert result.status == "completed"
    return settings


def _local_settings(store: SQLiteStore) -> V01ModelSettings:
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 10,
            "revision": 0,
        }
    )
    assert result.status == "completed"
    return settings


def _task(store: SQLiteStore, settings: V01ModelSettings) -> str:
    payload = settings.prepare_task_payload({"command": "Use the configured model."})
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id


def _policy() -> IntelligenceModePolicy:
    return IntelligenceModePolicy(
        external_api_enabled=True,
        external_provider_id="configured-api",
    )


def _client_factory(
    transport: _CountingTransport,
) -> Callable[..., httpx.AsyncClient]:
    mock = httpx.MockTransport(transport)

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=mock, **kwargs)

    return factory


def _runtime_request(task_id: str) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id="worker-thread",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"stage": "cloud-authority-composition"},
        },
    )


def _standing_authority(
    store: SQLiteStore,
    *,
    task_id: str,
    now: datetime,
) -> tuple[
    StandingPermissionStore,
    StandingPermissionBinding,
    StandingPermissionExecutionAuthority,
]:
    context = PermissionContext(
        user_id="user-1",
        project_id="project-1",
        task_id=task_id,
    )
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    permissions.grant(
        permission_id="cloud-model-permission",
        scope=StandingPermissionScope(
            subject_id="packaged-model-runtime",
            context=context,
            action_class="model.cloud.complete",
            targets=("configured-api",),
            sites=("api.example.test",),
            resources=("api-model",),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=now,
            expires_at=now + timedelta(hours=1),
        ),
    )
    binding = StandingPermissionBinding(
        permission_id="cloud-model-permission",
        subject_id="packaged-model-runtime",
        context=context,
        target="configured-api",
        resource_id="api-model",
        network_host="api.example.test",
    )
    authority = StandingPermissionExecutionAuthority(
        subject_id="packaged-model-runtime",
        context=context,
    )
    return permissions, binding, authority


def _factory(
    *,
    store: SQLiteStore,
    settings: V01ModelSettings,
    definitions: AgentDefinitionRepository,
    resolver: _CountingCredentialResolver,
    transport: _CountingTransport,
    authorizer: StandingPermissionCloudEffectAuthorizer | None = None,
    authority_resolver: (
        Callable[[str], StandingPermissionExecutionAuthority | None] | None
    ) = None,
) -> V01BoundModelRuntimeFactory:
    return V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        settings=settings,
        credential_resolver=resolver,
        client_factory=_client_factory(transport),
        intelligence_policy=_policy(),
        cloud_effect_authorizer=authorizer,
        cloud_execution_authority_resolver=authority_resolver,
    )


def test_real_standing_authority_reaches_cloud_provider_from_runtime_child_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    task_id = _task(store, settings)
    permissions, binding, authority = _standing_authority(
        store,
        task_id=task_id,
        now=now,
    )
    authorizer_task: list[asyncio.Task[object] | None] = []
    authority_requests: list[str] = []

    def binding_resolver(
        candidate: StandingPermissionExecutionAuthority,
    ) -> StandingPermissionBinding | None:
        authorizer_task.append(asyncio.current_task())
        return binding if candidate == authority else None

    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        binding_resolver,
        clock=lambda: now + timedelta(minutes=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=lambda candidate_task: (
            authority_requests.append(candidate_task)
            or (authority if candidate_task == task_id else None)
        ),
    ).for_task(task_id)
    assert runtime is not None

    async def scenario() -> object:
        caller_task = asyncio.current_task()
        result = await runtime.run(_runtime_request("team:team-authorized:worker-a"))
        assert authorizer_task
        assert authorizer_task[0] is not caller_task
        assert transport.tasks[0] is authorizer_task[0]
        return result

    result = asyncio.run(scenario())

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["provider_id"] == "configured-api"
    assert result.output["model"] == "api-model"
    assert resolver.references == ["env:NIKA_PACKAGED_CLOUD_KEY"]
    assert transport.calls == 1
    assert authority_requests == [task_id]


def test_real_authority_survives_canonical_multi_agent_member_identities(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    outer_task = _task(store, settings)
    permissions, binding, authority = _standing_authority(
        store,
        task_id=outer_task,
        now=now,
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda candidate: binding if candidate == authority else None,
        clock=lambda: now + timedelta(minutes=1),
    )
    authority_requests: list[str] = []
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    factory = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=lambda candidate_task: (
            authority_requests.append(candidate_task)
            or (authority if candidate_task == outer_task else None)
        ),
    )

    team_id = "authority-team"
    team_store = MultiAgentStore(store)
    team_store.create_team(
        team_id=team_id,
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread:authority-team:root",
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
            handoff_id="task:authority-team:root",
            correlation_id="team:authority-team:root",
        ),
    )
    supervisor = factory.supervisor_for_task(
        outer_task,
        store=team_store,
    )

    async def scenario() -> tuple[object, ...]:
        workers = await supervisor.fan_out(
            team_id=team_id,
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="worker-a",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread:authority-team:worker-a",
                    payload={"work": "Return worker A result."},
                ),
                ChildRequest(
                    member_id="worker-b",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread:authority-team:worker-b",
                    payload={"work": "Return worker B result."},
                ),
            ),
        )
        root = await supervisor.run_root_member(team_id=team_id, member_id="root")
        supervisor.finalize_team(team_id)
        return (*workers, root)

    executions = asyncio.run(scenario())

    assert len(executions) == 3
    assert all(
        execution.result is not None
        and execution.result.outcome is RuntimeOutcome.COMPLETED
        for execution in executions
    )
    assert authority_requests == [outer_task] * 3
    assert resolver.references == ["env:NIKA_PACKAGED_CLOUD_KEY"] * 3
    assert transport.calls == 3


def test_missing_host_authority_fails_before_credentials_or_transport(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    task_id = _task(store, settings)
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
    ).for_task(task_id)
    assert runtime is not None

    result = asyncio.run(runtime.run(_runtime_request(task_id)))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.output["model_error_code"] == "invalid_request"
    assert resolver.references == []
    assert transport.calls == 0


def test_wrong_task_authority_is_rejected_at_effect_admission(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    task_id = _task(store, settings)
    _permissions, binding, _authority = _standing_authority(
        store,
        task_id=task_id,
        now=now,
    )
    wrong_authority = StandingPermissionExecutionAuthority(
        subject_id="packaged-model-runtime",
        context=PermissionContext(
            user_id="user-1",
            project_id="project-1",
            task_id="different-task",
        ),
    )
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda _candidate: binding,
        clock=lambda: now + timedelta(minutes=1),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()

    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=lambda _task_id: wrong_authority,
    ).for_task(task_id)
    assert runtime is not None

    result = asyncio.run(runtime.run(_runtime_request(task_id)))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.output["model_error_code"] == "invalid_request"
    assert resolver.references == []
    assert transport.calls == 0


def test_revoked_authority_fails_before_credentials_or_transport(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    task_id = _task(store, settings)
    permissions, binding, authority = _standing_authority(
        store,
        task_id=task_id,
        now=now,
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda candidate: binding if candidate == authority else None,
        clock=lambda: now + timedelta(minutes=2),
    )
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=lambda candidate_task: (
            authority if candidate_task == task_id else None
        ),
    ).for_task(task_id)
    assert runtime is not None

    permissions.revoke(
        binding.permission_id,
        revoked_at=now + timedelta(minutes=1),
    )
    result = asyncio.run(runtime.run(_runtime_request(task_id)))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.output["model_error_code"] == "invalid_request"
    assert resolver.references == []
    assert transport.calls == 0


def test_member_execution_identity_cannot_retarget_outer_cloud_authority(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    settings = _settings(store)
    definitions = _definitions(store)
    outer_task = _task(store, settings)
    permissions, binding, authority = _standing_authority(
        store,
        task_id=outer_task,
        now=now,
    )
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda candidate: binding if candidate == authority else None,
        clock=lambda: now + timedelta(minutes=1),
    )
    authority_requests: list[str] = []
    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=lambda candidate_task: (
            authority_requests.append(candidate_task)
            or (authority if candidate_task == outer_task else None)
        ),
    ).for_task(outer_task)
    assert runtime is not None

    result = asyncio.run(
        runtime.run(_runtime_request("team:untrusted-correlation:worker-b"))
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert authority_requests == [outer_task]
    assert resolver.references == ["env:NIKA_PACKAGED_CLOUD_KEY"]
    assert transport.calls == 1


def test_local_route_never_consults_cloud_authority_resolver(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _local_settings(store)
    definitions = _definitions(store)
    task_id = _task(store, settings)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    authorizer = StandingPermissionCloudEffectAuthorizer(
        permissions,
        lambda _authority: None,
    )
    authority_calls: list[str] = []

    def unexpected_authority(task: str) -> None:
        authority_calls.append(task)
        raise AssertionError("local route must not resolve cloud authority")

    resolver = _CountingCredentialResolver()
    transport = _CountingTransport()
    runtime = _factory(
        store=store,
        settings=settings,
        definitions=definitions,
        resolver=resolver,
        transport=transport,
        authorizer=authorizer,
        authority_resolver=unexpected_authority,
    ).for_task(task_id)
    assert runtime is not None

    result = asyncio.run(runtime.run(_runtime_request(task_id)))

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["provider_id"] == "ollama"
    assert result.output["model"] == "qwen3:8b"
    assert authority_calls == []
    assert resolver.references == []
    assert transport.calls == 1
