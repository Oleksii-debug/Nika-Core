from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import MemberState, MultiAgentStore, MultiAgentSupervisor, TeamQuota
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeErrorCode,
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
    def __init__(
        self,
        *,
        fail_member: str | None = None,
        failed_result_member: str | None = None,
        raw_error: str = "raw runtime failure",
    ) -> None:
        self.fail_member = fail_member
        self.failed_result_member = failed_result_member
        self.raw_error = raw_error
        self.requests: list[RuntimeRequest] = []
        self.active = 0
        self.max_active = 0

    @property
    def runtime_id(self) -> str:
        return "v01-three-agent-test"

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
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if member_id == self.fail_member:
                raise RuntimeError("isolated worker failure")
            handoff = request.payload["handoff"]
            assert isinstance(handoff, dict)
            shared_task_id = str(handoff["shared_task_id"])
            if member_id == self.failed_result_member:
                return RuntimeResult(
                    outcome=RuntimeOutcome.FAILED,
                    error=self.raw_error,
                    error_code=RuntimeErrorCode.INTERNAL,
                )
            if member_id == "checker":
                observation = handoff["worker_observation"]
                assert isinstance(observation, dict)
                return RuntimeResult(
                    outcome=RuntimeOutcome.COMPLETED,
                    output={
                        "verdict": (
                            "accepted"
                            if observation["state"] == MemberState.COMPLETED.value
                            else "degraded"
                        ),
                        "checked_task_id": shared_task_id,
                        "worker_state": observation["state"],
                    },
                )
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output={
                    "evidence": "worker-evidence",
                    "shared_task_id": shared_task_id,
                },
            )
        finally:
            self.active -= 1

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError(f"unexpected resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


class SimulatedProcessCrash(RuntimeError):
    pass


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(
            ToolSpec("web.read", "Read web content", ToolRisk.READ_ONLY),
            ToolSpec("file.read", "Read workspace files", ToolRisk.READ_ONLY),
        ),
        model_profiles={"test"},
    )


def _definition(*, agent_id: str, grants: tuple[ToolGrant, ...]) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=1,
        name=agent_id,
        goal="Complete the fixed V0.1 team role.",
        instructions="Use only declared capabilities and return structured evidence.",
        model_profile="test",
        tool_grants=grants,
        enabled=True,
    )


def _save_and_activate(
    repository: AgentDefinitionRepository,
    definition: AgentDefinition,
) -> None:
    repository.save_draft(_compiler().compile(definition))
    repository.activate(definition)


def _grants() -> tuple[
    tuple[ToolGrant, ...],
    tuple[ToolGrant, ...],
    tuple[ToolGrant, ...],
]:
    return (
        (
            ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
            ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
        ),
        (ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),),
        (ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),),
    )


def _config(
    *,
    root_grants: tuple[ToolGrant, ...] | None = None,
    worker_grants: tuple[ToolGrant, ...] | None = None,
) -> V01ThreeAgentConfig:
    supervisor_grants, researcher_grants, checker_grants = _grants()
    return V01ThreeAgentConfig(
        root_member_id="supervisor",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_grants=supervisor_grants if root_grants is None else root_grants,
        worker=V01ChildAssignment(
            member_id="researcher",
            agent_id="researcher",
            agent_version=1,
            requested_grants=researcher_grants if worker_grants is None else worker_grants,
            instruction="Research the user goal and return evidence.",
        ),
        checker=V01ChildAssignment(
            member_id="checker",
            agent_id="checker-agent",
            agent_version=1,
            requested_grants=checker_grants,
            instruction="Review the worker evidence against the user goal.",
        ),
    )


def _open_existing(
    tmp_path: Path,
) -> tuple[RecordingRuntime, MultiAgentStore, V01ThreeAgentSupervisor]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = AgentDefinitionRepository(sqlite)
    runtime = RecordingRuntime()
    adapter = V01ThreeAgentSupervisor(
        coordinator=MultiAgentSupervisor(
            runtime=runtime,
            store=store,
            definitions=definitions,
        ),
        store=store,
        definitions=definitions,
        config=_config(),
    )
    return runtime, store, adapter


def _build(
    tmp_path: Path,
    *,
    fail_member: str | None = None,
    failed_result_member: str | None = None,
    raw_error: str = "raw runtime failure",
    root_grants: tuple[ToolGrant, ...] | None = None,
    worker_grants: tuple[ToolGrant, ...] | None = None,
) -> tuple[RecordingRuntime, MultiAgentStore, V01ThreeAgentSupervisor]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = AgentDefinitionRepository(sqlite)

    supervisor_grants, researcher_grants, checker_grants = _grants()
    _save_and_activate(
        definitions,
        _definition(agent_id="supervisor", grants=supervisor_grants),
    )
    _save_and_activate(
        definitions,
        _definition(agent_id="researcher", grants=researcher_grants),
    )
    _save_and_activate(
        definitions,
        _definition(agent_id="checker-agent", grants=checker_grants),
    )

    runtime = RecordingRuntime(
        fail_member=fail_member,
        failed_result_member=failed_result_member,
        raw_error=raw_error,
    )
    coordinator = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=definitions,
    )
    adapter = V01ThreeAgentSupervisor(
        coordinator=coordinator,
        store=store,
        definitions=definitions,
        config=_config(root_grants=root_grants, worker_grants=worker_grants),
    )
    return runtime, store, adapter


def test_fixed_three_agent_path_reaches_one_checked_terminal_result(tmp_path: Path) -> None:
    runtime, store, adapter = _build(tmp_path)

    result = asyncio.run(
        adapter.run(
            user_goal="Compare the controlled evidence and return one result.",
            shared_task_id="task-user-1",
            team_id="team-v01-1",
        )
    )

    assert [member.member_id for member in store.members("team-v01-1")] == [
        "supervisor",
        "researcher",
        "checker",
    ]
    assert store.quota("team-v01-1") == TeamQuota(
        max_depth=1,
        max_children_per_parent=2,
        max_total_agents=3,
        max_parallel=1,
    )
    assert len(runtime.requests) == 2
    assert runtime.max_active == 1

    worker_request, checker_request = runtime.requests
    worker_handoff = worker_request.payload["handoff"]
    checker_handoff = checker_request.payload["handoff"]
    assert isinstance(worker_handoff, dict)
    assert isinstance(checker_handoff, dict)
    assert worker_handoff["shared_task_id"] == "task-user-1"
    assert checker_handoff["shared_task_id"] == "task-user-1"
    assert checker_handoff["worker_observation"] == {
        "member_id": "researcher",
        "state": "completed",
        "output": {
            "evidence": "worker-evidence",
            "shared_task_id": "task-user-1",
        },
        "error": None,
    }
    assert worker_request.payload["tool_grants"][0]["tool_id"] == "web.read"
    assert checker_request.payload["tool_grants"][0]["tool_id"] == "file.read"

    assert result.shared_task_id == "task-user-1"
    assert result.team_state.value == "completed"
    assert result.worker.state is MemberState.COMPLETED
    assert result.checker.state is MemberState.COMPLETED
    assert result.final_output == {
        "status": "checked",
        "worker_output": {
            "evidence": "worker-evidence",
            "shared_task_id": "task-user-1",
        },
        "checker_output": {
            "verdict": "accepted",
            "checked_task_id": "task-user-1",
            "worker_state": "completed",
        },
        "worker_error": None,
        "checker_error": None,
    }


def test_one_worker_failure_is_isolated_and_checker_still_finishes(tmp_path: Path) -> None:
    runtime, store, adapter = _build(tmp_path, fail_member="researcher")

    result = asyncio.run(
        adapter.run(
            user_goal="Return a checked result even if research fails.",
            shared_task_id="task-user-failure",
            team_id="team-v01-failure",
        )
    )

    assert len(runtime.requests) == 2
    checker_handoff = runtime.requests[1].payload["handoff"]
    assert isinstance(checker_handoff, dict)
    assert checker_handoff["worker_observation"] == {
        "member_id": "researcher",
        "state": "failed",
        "output": {},
        "error": "RuntimeError",
    }
    assert result.worker.state is MemberState.FAILED
    assert result.worker.error == "RuntimeError"
    assert result.checker.state is MemberState.COMPLETED
    assert result.team_state.value == "completed"
    assert store.team_state("team-v01-failure").value == "completed"
    assert result.final_output == {
        "status": "degraded",
        "worker_output": {},
        "checker_output": {
            "verdict": "degraded",
            "checked_task_id": "task-user-failure",
            "worker_state": "failed",
        },
        "worker_error": "RuntimeError",
        "checker_error": None,
    }


def test_restart_after_worker_continues_same_team_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_runtime, first_store, first_adapter = _build(tmp_path)
    original_fan_out = first_adapter._coordinator.fan_out
    fan_out_calls = 0

    async def crash_before_checker(**kwargs: object):
        nonlocal fan_out_calls
        fan_out_calls += 1
        if fan_out_calls == 2:
            raise SimulatedProcessCrash("between worker and checker")
        return await original_fan_out(**kwargs)

    monkeypatch.setattr(first_adapter._coordinator, "fan_out", crash_before_checker)
    with pytest.raises(SimulatedProcessCrash, match="between worker and checker"):
        asyncio.run(
            first_adapter.run(
                user_goal="Inspect once, check once, return one result.",
                shared_task_id="task-restart-worker",
                team_id="team-restart-worker",
            )
        )

    assert len(first_runtime.requests) == 1
    assert [member.member_id for member in first_store.members("team-restart-worker")] == [
        "supervisor",
        "researcher",
    ]

    restarted_runtime, restarted_store, restarted_adapter = _open_existing(tmp_path)
    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Inspect once, check once, return one result.",
            shared_task_id="task-restart-worker",
            team_id="team-restart-worker",
        )
    )

    assert result.team_state.value == "completed"
    assert result.final_output["status"] == "checked"
    assert [request.payload["member_id"] for request in restarted_runtime.requests] == [
        "checker"
    ]
    assert [member.member_id for member in restarted_store.members("team-restart-worker")] == [
        "supervisor",
        "researcher",
        "checker",
    ]


def test_restart_after_checker_reconstructs_final_result_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_runtime, first_store, first_adapter = _build(tmp_path)

    def crash_before_finalize(team_id: str):
        assert team_id == "team-restart-finalize"
        raise SimulatedProcessCrash("after checker before finalize")

    monkeypatch.setattr(first_adapter._coordinator, "finalize_team", crash_before_finalize)
    with pytest.raises(SimulatedProcessCrash, match="after checker before finalize"):
        asyncio.run(
            first_adapter.run(
                user_goal="Inspect once, check once, return one result.",
                shared_task_id="task-restart-finalize",
                team_id="team-restart-finalize",
            )
        )

    assert len(first_runtime.requests) == 2
    assert first_store.team_state("team-restart-finalize").value == "active"

    restarted_runtime, restarted_store, restarted_adapter = _open_existing(tmp_path)
    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Inspect once, check once, return one result.",
            shared_task_id="task-restart-finalize",
            team_id="team-restart-finalize",
        )
    )

    assert restarted_runtime.requests == []
    assert restarted_store.team_state("team-restart-finalize").value == "completed"
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


def test_restart_rejects_changed_logical_task_identity(tmp_path: Path) -> None:
    first_runtime, _first_store, first_adapter = _build(tmp_path)
    asyncio.run(
        first_adapter.run(
            user_goal="One exact goal.",
            shared_task_id="task-exact",
            team_id="team-exact",
        )
    )
    assert len(first_runtime.requests) == 2

    restarted_runtime, _store, restarted_adapter = _open_existing(tmp_path)
    with pytest.raises(PermissionError, match="task identity"):
        asyncio.run(
            restarted_adapter.run(
                user_goal="Changed goal.",
                shared_task_id="task-changed",
                team_id="team-exact",
            )
        )
    assert restarted_runtime.requests == []


def test_runtime_result_error_is_bounded_before_storage_and_projection(tmp_path: Path) -> None:
    canary = "PROVIDER-SECRET-IN-RUNTIME-ERROR"
    runtime, _store, adapter = _build(
        tmp_path,
        failed_result_member="researcher",
        raw_error=canary,
    )

    result = asyncio.run(
        adapter.run(
            user_goal="Contain provider failure text.",
            shared_task_id="task-safe-error",
            team_id="team-safe-error",
        )
    )

    assert result.worker.state is MemberState.FAILED
    assert result.worker.error == RuntimeErrorCode.INTERNAL.value
    checker_handoff = runtime.requests[1].payload["handoff"]
    assert isinstance(checker_handoff, dict)
    assert checker_handoff["worker_observation"]["error"] == RuntimeErrorCode.INTERNAL.value
    serialized_result = json.dumps(result.final_output, ensure_ascii=False, sort_keys=True)
    assert canary not in serialized_result

    with SQLiteStore(tmp_path / "nika.db").connection() as conn:
        durable_dump = "\n".join(conn.iterdump())
    assert canary not in durable_dump


def test_child_permission_expansion_fails_before_runtime_execution(tmp_path: Path) -> None:
    broader_worker_grants = (
        ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
    )
    runtime, store, adapter = _build(tmp_path, worker_grants=broader_worker_grants)

    with pytest.raises(PermissionError, match="ungranted tool"):
        asyncio.run(
            adapter.run(
                user_goal="Do not expand child permissions.",
                shared_task_id="task-permission",
                team_id="team-permission",
            )
        )

    assert runtime.requests == []
    assert [member.member_id for member in store.members("team-permission")] == ["supervisor"]


def test_root_permission_expansion_fails_before_team_creation(tmp_path: Path) -> None:
    broader_root_grants = (
        ToolGrant(tool_id="shell.exec", max_risk=1, scopes=("workspace",)),
    )
    runtime, store, adapter = _build(tmp_path, root_grants=broader_root_grants)

    with pytest.raises(PermissionError, match="ungranted tool"):
        asyncio.run(
            adapter.run(
                user_goal="Do not expand root permissions.",
                shared_task_id="task-root-permission",
                team_id="team-root-permission",
            )
        )

    assert runtime.requests == []
    with pytest.raises(KeyError, match="unknown team"):
        store.team_state("team-root-permission")
