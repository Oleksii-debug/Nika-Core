from __future__ import annotations

import asyncio
from pathlib import Path

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MemberState, MultiAgentStore, MultiAgentSupervisor, TeamQuota
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)


class SlowResumeRuntime(AgentRuntimePort):
    def __init__(self) -> None:
        self.resume_requests: list[RuntimeResumeRequest] = []

    @property
    def runtime_id(self) -> str:
        return "slow-resume-test"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset({RuntimeCapability.DURABLE_RESUME})

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError(f"running child must resume, not restart: {request.task_id}")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_requests.append(request)
        await asyncio.sleep(0.02)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


def _definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    compiler = AgentCompiler(tools=(), model_profiles={"test"})
    for agent_id in ("supervisor", "worker"):
        definition = AgentDefinition(
            agent_id=agent_id,
            version=1,
            name=agent_id,
            goal="Recover durable team work exactly once.",
            instructions="Preserve restart identity and do not duplicate active recovery.",
            model_profile="test",
            tool_grants=(),
        )
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _running_child(path: Path) -> tuple[MultiAgentStore, AgentDefinitionRepository]:
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = _definitions(sqlite)
    store.create_team(
        team_id="team-concurrent-recovery",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=4,
            max_parallel=2,
        ),
    )
    store.spawn_child(
        team_id="team-concurrent-recovery",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(),
    )
    store.prepare_member_execution(
        team_id="team-concurrent-recovery",
        member_id="child",
        resume_token="thread-child",
    )
    return store, definitions


def test_concurrent_recover_team_calls_issue_one_runtime_resume(tmp_path: Path) -> None:
    store, definitions = _running_child(tmp_path / "concurrent-recovery.db")
    runtime = SlowResumeRuntime()
    supervisor = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)

    async def recover_twice() -> tuple[tuple[object, ...], tuple[object, ...]]:
        first, second = await asyncio.gather(
            supervisor.recover_team("team-concurrent-recovery"),
            supervisor.recover_team("team-concurrent-recovery"),
        )
        return first, second

    first, second = asyncio.run(recover_twice())

    assert sorted((len(first), len(second))) == [0, 1]
    assert len(runtime.resume_requests) == 1
    assert runtime.resume_requests[0].resume_token == "thread-child"
    assert store.member("team-concurrent-recovery", "child").state is MemberState.COMPLETED
    assert asyncio.run(supervisor.recover_team("team-concurrent-recovery")) == ()
    assert len(runtime.resume_requests) == 1
