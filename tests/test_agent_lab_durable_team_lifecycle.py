from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    ChildRequest,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
    TeamState,
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


class SimulatedProcessLoss(BaseException):
    pass


class DurableRecoveryRuntime(AgentRuntimePort):
    def __init__(
        self,
        *,
        crash_on_run: bool = False,
        waiting_approval: bool = False,
        fail_members: frozenset[str] = frozenset(),
    ) -> None:
        self.crash_on_run = crash_on_run
        self.waiting_approval = waiting_approval
        self.fail_members = fail_members
        self.run_requests: list[RuntimeRequest] = []
        self.resume_requests: list[RuntimeResumeRequest] = []
        self.cancelled: list[tuple[str, str]] = []

    @property
    def runtime_id(self) -> str:
        return "durable-recovery-test"

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
        self.run_requests.append(request)
        if self.crash_on_run:
            raise SimulatedProcessLoss("process disappeared after durable start binding")
        member_id = str(request.payload["member_id"])
        if member_id in self.fail_members:
            raise RuntimeError("worker failed")
        if self.waiting_approval:
            return RuntimeResult(
                outcome=RuntimeOutcome.WAITING_APPROVAL,
                output={"member_id": member_id, "approval": "required"},
                resume_token=request.thread_id,
            )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"member_id": member_id, "ok": True},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_requests.append(request)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancelled.append((task_id, thread_id))
        return True


class LyingDurableRuntime(AgentRuntimePort):
    @property
    def runtime_id(self) -> str:
        return "lying-durable"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset({RuntimeCapability.DURABLE_RESUME})

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


def _sqlite(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _grants() -> tuple[ToolGrant, ...]:
    return (ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),)


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(ToolSpec("web.read", "Read web content", ToolRisk.READ_ONLY),),
        model_profiles={"test"},
    )


def _activate_definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            version=1,
            name=agent_id,
            goal="Complete durable team work.",
            instructions="Use only the declared capability and preserve restart evidence.",
            model_profile="test",
            tool_grants=_grants(),
        )
        repository.save_draft(_compiler().compile(definition))
        repository.activate(definition)
    return repository


def _team(store: MultiAgentStore) -> None:
    store.create_team(
        team_id="team-recovery",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=_grants(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=4,
            max_total_agents=8,
            max_parallel=2,
        ),
    )


def _request(member_id: str = "child") -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=_grants(),
        payload={"query": f"task-{member_id}"},
    )


def test_process_loss_after_start_binding_recovers_via_runtime_resume(tmp_path: Path) -> None:
    path = tmp_path / "process-loss.db"
    sqlite = _sqlite(path)
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    crashing = DurableRecoveryRuntime(crash_on_run=True)
    supervisor = MultiAgentSupervisor(runtime=crashing, store=store, definitions=definitions)

    with pytest.raises(SimulatedProcessLoss):
        asyncio.run(
            supervisor.fan_out(
                team_id="team-recovery",
                parent_id="root",
                requests=(_request(),),
            )
        )

    stranded = store.member("team-recovery", "child")
    assert stranded.state is MemberState.RUNNING
    assert stranded.resume_token == "thread-child"
    assert store.task_payload("team-recovery", "child") == {"query": "task-child"}

    reloaded_sqlite = _sqlite(path)
    reloaded = MultiAgentStore(reloaded_sqlite)
    recovering_runtime = DurableRecoveryRuntime()
    recovering = MultiAgentSupervisor(
        runtime=recovering_runtime,
        store=reloaded,
        definitions=AgentDefinitionRepository(reloaded_sqlite),
    )
    executions = asyncio.run(recovering.recover_team("team-recovery"))

    assert len(executions) == 1
    assert executions[0].result is not None
    assert executions[0].result.outcome is RuntimeOutcome.COMPLETED
    assert reloaded.member("team-recovery", "child").state is MemberState.COMPLETED
    assert len(recovering_runtime.resume_requests) == 1
    assert recovering_runtime.resume_requests[0].resume_token == "thread-child"
    with reloaded_sqlite.connection() as conn:
        result_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM multi_agent_results "
                "WHERE team_id = ? AND member_id = ?",
                ("team-recovery", "child"),
            ).fetchone()[0]
        )
    assert result_count == 1


def test_spawned_child_recovers_from_persisted_task_without_resume(tmp_path: Path) -> None:
    path = tmp_path / "spawned.db"
    sqlite = _sqlite(path)
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    store.spawn_child(
        team_id="team-recovery",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=_grants(),
        task_handoff=AgentHandoff(
            team_id="team-recovery",
            sender_id="root",
            recipient_id="child",
            kind=HandoffKind.TASK,
            payload={"query": "persisted-before-run"},
        ),
    )

    runtime = DurableRecoveryRuntime()
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)
    executions = asyncio.run(supervisor.recover_team("team-recovery"))

    assert len(executions) == 1
    assert len(runtime.run_requests) == 1
    assert runtime.resume_requests == []
    assert runtime.run_requests[0].payload["handoff"] == {"query": "persisted-before-run"}
    assert store.member("team-recovery", "child").state is MemberState.COMPLETED


def test_recovery_does_not_bypass_waiting_approval(tmp_path: Path) -> None:
    path = tmp_path / "approval.db"
    sqlite = _sqlite(path)
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    runtime = DurableRecoveryRuntime(waiting_approval=True)
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)

    asyncio.run(
        supervisor.fan_out(
            team_id="team-recovery",
            parent_id="root",
            requests=(_request(),),
        )
    )
    waiting = store.member("team-recovery", "child")
    assert waiting.state is MemberState.WAITING_APPROVAL
    assert waiting.resume_token == "thread-child"

    after_restart = DurableRecoveryRuntime()
    restarted = MultiAgentSupervisor(
        runtime=after_restart,
        store=MultiAgentStore(_sqlite(path)),
        definitions=AgentDefinitionRepository(_sqlite(path)),
    )
    assert asyncio.run(restarted.recover_team("team-recovery")) == ()
    assert after_restart.resume_requests == []
    with pytest.raises(RuntimeError, match="nonterminal"):
        restarted.finalize_team("team-recovery")


def test_durable_runtime_without_initial_cursor_fails_before_spawn(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path / "lying.db")
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    supervisor = MultiAgentSupervisor(
        runtime=LyingDurableRuntime(),
        store=store,
        definitions=definitions,
    )

    with pytest.raises(TypeError, match="initial_resume_token"):
        asyncio.run(
            supervisor.fan_out(
                team_id="team-recovery",
                parent_id="root",
                requests=(_request(),),
            )
        )
    assert [member.member_id for member in store.members("team-recovery")] == ["root"]


def test_explicit_team_finalization_preserves_contained_worker_failure(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path / "mixed.db")
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    supervisor = MultiAgentSupervisor(
        runtime=DurableRecoveryRuntime(fail_members=frozenset({"bad"})),
        store=store,
        definitions=definitions,
    )

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-recovery",
            parent_id="root",
            requests=(_request("good"), _request("bad")),
        )
    )
    assert {item.member.member_id: item.member.state for item in executions} == {
        "good": MemberState.COMPLETED,
        "bad": MemberState.FAILED,
    }

    assert supervisor.finalize_team("team-recovery") is TeamState.COMPLETED
    assert store.team_state("team-recovery") is TeamState.COMPLETED
    with pytest.raises(RuntimeError, match="team is not active"):
        store.spawn_child(
            team_id="team-recovery",
            parent_id="root",
            child_id="late",
            agent_id="worker",
            agent_version=1,
            thread_id="thread-late",
            requested_grants=_grants(),
        )


def test_team_with_only_failed_children_finalizes_failed(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path / "failed.db")
    store = MultiAgentStore(sqlite)
    _team(store)
    definitions = _activate_definitions(sqlite)
    supervisor = MultiAgentSupervisor(
        runtime=DurableRecoveryRuntime(fail_members=frozenset({"bad"})),
        store=store,
        definitions=definitions,
    )

    asyncio.run(
        supervisor.fan_out(
            team_id="team-recovery",
            parent_id="root",
            requests=(_request("bad"),),
        )
    )

    assert supervisor.finalize_team("team-recovery") is TeamState.FAILED
    assert store.team_state("team-recovery") is TeamState.FAILED
