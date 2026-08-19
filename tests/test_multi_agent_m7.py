from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    ChildRequest,
    EvaluationScore,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
)
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.tools import ToolRisk, ToolSpec


class FakeRuntime(AgentRuntimePort):
    def __init__(
        self,
        *,
        fail_member: str | None = None,
        failure_type: type[Exception] = RuntimeError,
    ) -> None:
        self.fail_member = fail_member
        self.failure_type = failure_type
        self.active = 0
        self.max_active = 0
        self.cancelled: list[tuple[str, str]] = []
        self.resumed: list[RuntimeResumeRequest] = []

    @property
    def runtime_id(self) -> str:
        return "fake-m7"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset(
            {
                RuntimeCapability.DURABLE_RESUME,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.PARALLELISM,
                RuntimeCapability.SUBAGENTS,
            }
        )

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        member_id = str(request.payload["member_id"])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if member_id == self.fail_member:
                raise self.failure_type("isolated worker failure")
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output={"member_id": member_id, "ok": True},
            )
        finally:
            self.active -= 1

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resumed.append(request)
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"resumed": request.task_id})

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        return True


def make_store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    return sqlite, MultiAgentStore(sqlite)


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(
            ToolSpec("web.read", "Read web content", ToolRisk.READ_ONLY),
            ToolSpec("file.read", "Read workspace files", ToolRisk.READ_ONLY),
        ),
        model_profiles={"test"},
    )


def _definition(
    *,
    agent_id: str,
    version: int = 1,
    grants: tuple[ToolGrant, ...] = (),
    enabled: bool = True,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=version,
        name=agent_id,
        goal="Complete the assigned team task.",
        instructions="Use only the declared capabilities and return structured evidence.",
        model_profile="test",
        tool_grants=grants,
        enabled=enabled,
    )


def _save_and_activate(
    repository: AgentDefinitionRepository,
    definition: AgentDefinition,
) -> AgentDefinition:
    repository.save_draft(_compiler().compile(definition))
    repository.activate(definition)
    return definition


def _definitions(
    sqlite: SQLiteStore,
    *,
    worker_grants: tuple[ToolGrant, ...] | None = None,
) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    root_grants = (
        ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com", "docs.example.com")),
        ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
    )
    _save_and_activate(
        repository,
        _definition(agent_id="supervisor", grants=root_grants),
    )
    _save_and_activate(
        repository,
        _definition(
            agent_id="worker",
            grants=(
                ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
            )
            if worker_grants is None
            else worker_grants,
        ),
    )
    return repository


def create_team(store: MultiAgentStore, *, max_parallel: int = 2) -> tuple[ToolGrant, ...]:
    root_grants = (
        ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com", "docs.example.com")),
        ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
    )
    store.create_team(
        team_id="team-1",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=root_grants,
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=4,
            max_total_agents=6,
            max_parallel=max_parallel,
        ),
    )
    return root_grants


def test_schema_v7_and_restart_safe_lineage(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    assert sqlite.schema_version() == 7
    create_team(store)
    child = store.spawn_child(
        team_id="team-1",
        parent_id="root",
        child_id="researcher",
        agent_id="researcher",
        agent_version=2,
        thread_id="thread-researcher",
        requested_grants=(
            ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
        ),
    )
    store.set_member_state(
        team_id="team-1",
        member_id="researcher",
        state=MemberState.WAITING_APPROVAL,
        resume_token="resume-123",
    )

    reloaded = MultiAgentStore(SQLiteStore(sqlite.path))
    recovered = reloaded.recoverable_members("team-1")
    assert child.parent_id == "root"
    assert [(item.member_id, item.resume_token) for item in recovered] == [
        ("root", None),
        ("researcher", "resume-123"),
    ]


def test_privilege_escalation_and_quotas_fail_closed(tmp_path: Path) -> None:
    _, store = make_store(tmp_path)
    create_team(store)

    with pytest.raises(PermissionError, match="ungranted tool"):
        store.spawn_child(
            team_id="team-1",
            parent_id="root",
            child_id="unsafe",
            agent_id="unsafe",
            agent_version=1,
            thread_id="thread-unsafe",
            requested_grants=(ToolGrant(tool_id="shell.exec", max_risk=1),),
        )

    with pytest.raises(PermissionError, match="broader scope"):
        store.spawn_child(
            team_id="team-1",
            parent_id="root",
            child_id="wide",
            agent_id="wide",
            agent_version=1,
            thread_id="thread-wide",
            requested_grants=(
                ToolGrant(tool_id="web.read", max_risk=0, scopes=("other.example",)),
            ),
        )

    child = store.spawn_child(
        team_id="team-1",
        parent_id="root",
        child_id="child",
        agent_id="child",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),),
    )
    assert child.depth == 1
    grandchild = store.spawn_child(
        team_id="team-1",
        parent_id="child",
        child_id="grandchild",
        agent_id="grandchild",
        agent_version=1,
        thread_id="thread-grandchild",
        requested_grants=(),
    )
    assert grandchild.depth == 2
    with pytest.raises(RuntimeError, match="depth"):
        store.spawn_child(
            team_id="team-1",
            parent_id="grandchild",
            child_id="too-deep",
            agent_id="too-deep",
            agent_version=1,
            thread_id="thread-too-deep",
            requested_grants=(),
        )


def test_bounded_fanout_contains_worker_failure(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store, max_parallel=2)
    definitions = _definitions(sqlite)
    runtime = FakeRuntime(fail_member="child-2")
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)
    requests = tuple(
        ChildRequest(
            member_id=f"child-{index}",
            agent_id="worker",
            agent_version=1,
            thread_id=f"thread-{index}",
            requested_grants=(
                ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
            ),
            payload={"index": index},
        )
        for index in range(4)
    )

    executions = asyncio.run(
        supervisor.fan_out(team_id="team-1", parent_id="root", requests=requests)
    )

    assert runtime.max_active == 2
    assert [item.exception for item in executions].count("RuntimeError") == 1
    states = {item.member_id: item.state for item in store.members("team-1")}
    assert states["child-2"] == MemberState.FAILED
    assert states["child-0"] == MemberState.COMPLETED
    assert states["child-1"] == MemberState.COMPLETED
    assert states["child-3"] == MemberState.COMPLETED


def test_non_runtimeerror_worker_failure_is_isolated(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store)
    definitions = _definitions(sqlite)
    runtime = FakeRuntime(fail_member="child-0", failure_type=ValueError)
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-1",
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="child-0",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-0",
                    requested_grants=(
                        ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
                    ),
                ),
            ),
        )
    )

    assert executions[0].exception == "ValueError"
    assert store.member("team-1", "child-0").state == MemberState.FAILED


def test_fanout_rejects_non_active_child_definition_before_spawning(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store)
    definitions = AgentDefinitionRepository(sqlite)
    _save_and_activate(definitions, _definition(agent_id="supervisor", grants=create_team_grants()))
    draft = _definition(
        agent_id="worker",
        grants=(ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),),
    )
    definitions.save_draft(_compiler().compile(draft))
    supervisor = MultiAgentSupervisor(runtime=FakeRuntime(), store=store, definitions=definitions)

    with pytest.raises(PermissionError, match="not active"):
        asyncio.run(
            supervisor.fan_out(
                team_id="team-1",
                parent_id="root",
                requests=(
                    ChildRequest(
                        member_id="child",
                        agent_id="worker",
                        agent_version=1,
                        thread_id="thread-child",
                        requested_grants=(
                            ToolGrant(
                                tool_id="web.read",
                                max_risk=0,
                                scopes=("example.com",),
                            ),
                        ),
                    ),
                ),
            )
        )
    assert [member.member_id for member in store.members("team-1")] == ["root"]


def create_team_grants() -> tuple[ToolGrant, ...]:
    return (
        ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com", "docs.example.com")),
        ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
    )


def test_fanout_rejects_grants_not_declared_by_child_definition(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store)
    definitions = _definitions(
        sqlite,
        worker_grants=(ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),),
    )
    supervisor = MultiAgentSupervisor(runtime=FakeRuntime(), store=store, definitions=definitions)

    with pytest.raises(PermissionError, match="activated definition"):
        asyncio.run(
            supervisor.fan_out(
                team_id="team-1",
                parent_id="root",
                requests=(
                    ChildRequest(
                        member_id="child",
                        agent_id="worker",
                        agent_version=1,
                        thread_id="thread-child",
                        requested_grants=(
                            ToolGrant(
                                tool_id="web.read",
                                max_risk=0,
                                scopes=("example.com",),
                            ),
                        ),
                    ),
                ),
            )
        )
    assert [member.member_id for member in store.members("team-1")] == ["root"]


def test_duplicate_fanout_identity_fails_before_partial_spawn(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store)
    definitions = _definitions(sqlite)
    supervisor = MultiAgentSupervisor(runtime=FakeRuntime(), store=store, definitions=definitions)
    duplicate = ChildRequest(
        member_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(
            ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
        ),
    )

    with pytest.raises(ValueError, match="duplicate child member_id"):
        asyncio.run(
            supervisor.fan_out(
                team_id="team-1",
                parent_id="root",
                requests=(duplicate, duplicate),
            )
        )
    assert [member.member_id for member in store.members("team-1")] == ["root"]


def test_cancel_propagates_to_recoverable_children(tmp_path: Path) -> None:
    sqlite, store = make_store(tmp_path)
    create_team(store)
    for index in range(2):
        store.spawn_child(
            team_id="team-1",
            parent_id="root",
            child_id=f"child-{index}",
            agent_id="worker",
            agent_version=1,
            thread_id=f"thread-{index}",
            requested_grants=(),
        )
        store.set_member_state(
            team_id="team-1",
            member_id=f"child-{index}",
            state=MemberState.RUNNING,
        )

    runtime = FakeRuntime()
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=AgentDefinitionRepository(sqlite),
    )
    members = asyncio.run(supervisor.cancel_team("team-1"))

    assert {task_id for task_id, _ in runtime.cancelled} == {
        "team:team-1:root",
        "team:team-1:child-0",
        "team:team-1:child-1",
    }
    assert all(member.state == MemberState.CANCELLED for member in members)


def test_evaluator_aggregation_is_deterministic() -> None:
    scores = (
        EvaluationScore(evaluator_id="e1", target_member_id="a", score=0.8),
        EvaluationScore(evaluator_id="e2", target_member_id="a", score=1.0),
        EvaluationScore(evaluator_id="e1", target_member_id="b", score=0.5),
    )
    assert MultiAgentSupervisor.aggregate_evaluations(scores) == {"a": 0.9, "b": 0.5}
