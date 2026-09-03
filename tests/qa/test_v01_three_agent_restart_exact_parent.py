from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MemberState, MultiAgentStore, MultiAgentSupervisor, TeamState
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


class SimulatedProcessCrash(RuntimeError):
    pass


class DeterministicRuntime(AgentRuntimePort):
    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []

    @property
    def runtime_id(self) -> str:
        return "worker41-v01-b02-restart-oracle"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset({RuntimeCapability.PARALLELISM, RuntimeCapability.SUBAGENTS})

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        member_id = str(request.payload["member_id"])
        handoff = request.payload["handoff"]
        assert isinstance(handoff, dict)
        shared_task_id = str(handoff["shared_task_id"])
        if member_id == "checker":
            worker_observation = handoff["worker_observation"]
            assert isinstance(worker_observation, dict)
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output={
                    "verdict": "accepted",
                    "checked_task_id": shared_task_id,
                    "worker_state": worker_observation["state"],
                },
            )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"evidence": "worker-evidence", "shared_task_id": shared_task_id},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError(f"unexpected child runtime resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(ToolSpec("file.read", "Read fixture", ToolRisk.READ_ONLY),),
        model_profiles={"test"},
    )


def _definition(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=1,
        name=agent_id,
        goal="Complete one deterministic V0.1 role.",
        instructions="Use only the declared read-only fixture capability.",
        model_profile="test",
        tool_grants=(ToolGrant(tool_id="file.read", max_risk=0, scopes=("fixture",)),),
        enabled=True,
    )


def _initialize_definitions(db_path: Path) -> None:
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    definitions = AgentDefinitionRepository(sqlite)
    compiler = _compiler()
    for agent_id in ("supervisor", "worker", "checker-agent"):
        definition = _definition(agent_id)
        definitions.save_draft(compiler.compile(definition))
        definitions.activate(definition)


def _config() -> V01ThreeAgentConfig:
    grant = ToolGrant(tool_id="file.read", max_risk=0, scopes=("fixture",))
    return V01ThreeAgentConfig(
        root_member_id="supervisor",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_grants=(grant,),
        worker=V01ChildAssignment(
            member_id="worker",
            agent_id="worker",
            agent_version=1,
            requested_grants=(grant,),
            instruction="Inspect the controlled fixture and return evidence.",
        ),
        checker=V01ChildAssignment(
            member_id="checker",
            agent_id="checker-agent",
            agent_version=1,
            requested_grants=(grant,),
            instruction="Independently check the worker evidence.",
        ),
    )


def _open(
    db_path: Path,
) -> tuple[DeterministicRuntime, MultiAgentStore, MultiAgentSupervisor, V01ThreeAgentSupervisor]:
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = AgentDefinitionRepository(sqlite)
    runtime = DeterministicRuntime()
    coordinator = MultiAgentSupervisor(runtime=runtime, store=store, definitions=definitions)
    adapter = V01ThreeAgentSupervisor(
        coordinator=coordinator,
        store=store,
        definitions=definitions,
        config=_config(),
    )
    return runtime, store, coordinator, adapter


def test_restart_after_worker_before_checker_continues_same_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process crash between stages must not strand the durable logical task."""
    db_path = tmp_path / "nika.db"
    _initialize_definitions(db_path)
    first_runtime, first_store, first_coordinator, first_adapter = _open(db_path)
    original_fan_out = first_coordinator.fan_out
    fan_out_calls = 0

    async def crash_before_checker(**kwargs: object):
        nonlocal fan_out_calls
        fan_out_calls += 1
        if fan_out_calls == 2:
            raise SimulatedProcessCrash("between worker and checker")
        return await original_fan_out(**kwargs)

    monkeypatch.setattr(first_coordinator, "fan_out", crash_before_checker)
    with pytest.raises(SimulatedProcessCrash, match="between worker and checker"):
        asyncio.run(
            first_adapter.run(
                user_goal="Inspect once, check once, return one team result.",
                shared_task_id="task-restart-mid-stage",
                team_id="team-restart-mid-stage",
            )
        )

    assert [member.state for member in first_store.members("team-restart-mid-stage")] == [
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]
    assert [request.payload["member_id"] for request in first_runtime.requests] == [
        "supervisor",
        "worker",
    ]

    restarted_runtime, restarted_store, restarted_coordinator, restarted_adapter = _open(db_path)
    assert asyncio.run(restarted_coordinator.recover_team("team-restart-mid-stage")) == ()

    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Inspect once, check once, return one team result.",
            shared_task_id="task-restart-mid-stage",
            team_id="team-restart-mid-stage",
        )
    )

    assert result.team_state is TeamState.COMPLETED
    assert result.final_output["status"] == "checked"
    assert [member.member_id for member in restarted_store.members("team-restart-mid-stage")] == [
        "supervisor",
        "worker",
        "checker",
    ]
    assert len(restarted_runtime.requests) == 1
    assert restarted_runtime.requests[0].payload["member_id"] == "checker"


def test_restart_after_checker_before_finalize_reconstructs_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal child evidence must yield the same final result after process restart."""
    db_path = tmp_path / "nika.db"
    _initialize_definitions(db_path)
    first_runtime, first_store, first_coordinator, first_adapter = _open(db_path)

    def crash_before_finalize(team_id: str) -> TeamState:
        assert team_id == "team-restart-finalize"
        raise SimulatedProcessCrash("after checker before final result")

    monkeypatch.setattr(first_coordinator, "finalize_team", crash_before_finalize)
    with pytest.raises(SimulatedProcessCrash, match="after checker before final result"):
        asyncio.run(
            first_adapter.run(
                user_goal="Inspect once, check once, return one team result.",
                shared_task_id="task-restart-finalize",
                team_id="team-restart-finalize",
            )
        )

    assert [request.payload["member_id"] for request in first_runtime.requests] == [
        "supervisor",
        "worker",
        "checker",
    ]
    assert first_store.team_state("team-restart-finalize") is TeamState.ACTIVE
    assert [member.state for member in first_store.members("team-restart-finalize")] == [
        MemberState.COMPLETED,
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]

    restarted_runtime, restarted_store, restarted_coordinator, restarted_adapter = _open(db_path)
    assert asyncio.run(restarted_coordinator.recover_team("team-restart-finalize")) == ()

    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Inspect once, check once, return one team result.",
            shared_task_id="task-restart-finalize",
            team_id="team-restart-finalize",
        )
    )

    assert restarted_runtime.requests == []
    assert result.team_state is TeamState.COMPLETED
    assert result.final_output == {
        "status": "checked",
        "worker_output": {
            "evidence": "worker-evidence",
            "shared_task_id": "task-restart-finalize",
        },
        "checker_output": {
            "verdict": "accepted",
            "checked_task_id": "task-restart-finalize",
            "worker_state": "completed",
        },
        "worker_error": None,
        "checker_error": None,
    }
    assert restarted_store.team_state("team-restart-finalize") is TeamState.COMPLETED
