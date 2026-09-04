from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MultiAgentStore, MultiAgentSupervisor
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.tools import ToolRisk, ToolSpec
from nika_core.v01_three_agent_supervisor import (
    V01ChildAssignment,
    V01ThreeAgentConfig,
    V01ThreeAgentSupervisor,
)


class RecordingRuntime(AgentRuntimePort):
    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []

    @property
    def runtime_id(self) -> str:
        return "v01-three-runtime-members-qa"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset(
            {
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.PARALLELISM,
                RuntimeCapability.SUBAGENTS,
            }
        )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        member_id = str(request.payload["member_id"])
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"runtime_member_id": member_id},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError(f"unexpected resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


def _grant() -> ToolGrant:
    return ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",))


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(ToolSpec("web.read", "Read controlled web content", ToolRisk.READ_ONLY),),
        model_profiles={"test"},
    )


def _definition(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=1,
        name=agent_id,
        goal="Execute one bounded V0.1 team role.",
        instructions="Return deterministic controlled evidence.",
        model_profile="test",
        tool_grants=(_grant(),),
        enabled=True,
    )


def _activate(repository: AgentDefinitionRepository, agent_id: str) -> None:
    definition = _definition(agent_id)
    repository.save_draft(_compiler().compile(definition))
    repository.activate(definition)


def _build(tmp_path: Path) -> tuple[RecordingRuntime, MultiAgentStore, V01ThreeAgentSupervisor]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = AgentDefinitionRepository(sqlite)
    for agent_id in ("supervisor", "researcher", "checker-agent"):
        _activate(definitions, agent_id)

    runtime = RecordingRuntime()
    coordinator = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=definitions,
    )
    grant = _grant()
    adapter = V01ThreeAgentSupervisor(
        coordinator=coordinator,
        store=store,
        definitions=definitions,
        config=V01ThreeAgentConfig(
            root_member_id="supervisor",
            root_agent_id="supervisor",
            root_agent_version=1,
            root_grants=(grant,),
            worker=V01ChildAssignment(
                member_id="researcher",
                agent_id="researcher",
                agent_version=1,
                requested_grants=(grant,),
                instruction="Inspect the controlled source and return evidence.",
            ),
            checker=V01ChildAssignment(
                member_id="checker",
                agent_id="checker-agent",
                agent_version=1,
                requested_grants=(grant,),
                instruction="Check the controlled worker evidence.",
            ),
        ),
    )
    return runtime, store, adapter


def test_representative_three_agent_journey_executes_all_three_members_through_runtime(
    tmp_path: Path,
) -> None:
    runtime, store, adapter = _build(tmp_path)

    result = asyncio.run(
        adapter.run(
            user_goal="Inspect controlled evidence and return a checked result.",
            shared_task_id="task-three-runtime-members",
            team_id="team-three-runtime-members",
        )
    )

    durable_member_ids = [
        member.member_id for member in store.members("team-three-runtime-members")
    ]
    runtime_member_ids = [str(request.payload["member_id"]) for request in runtime.requests]

    assert durable_member_ids == ["supervisor", "researcher", "checker"]
    assert result.team_state.value == "completed"
    assert Counter(runtime_member_ids) == Counter(
        {"supervisor": 1, "researcher": 1, "checker": 1}
    ), (
        "V0.1 stores three member identities but must also execute each representative "
        f"member through AgentRuntimePort exactly once; observed={runtime_member_ids!r}"
    )
