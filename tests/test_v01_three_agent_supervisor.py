from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    AgentHandoff,
    CheckerStatus,
    HandoffKind,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    SourceInspectionAssignment,
    TeamQuota,
    TeamState,
    V01CheckerAgent,
    encode_source_result,
)
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
    SourceSpec,
)
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
    V01SourceWorkerAssignment,
    V01ThreeAgentConfig,
    V01ThreeAgentSupervisor,
)


class RecordingScenarioRuntime(AgentRuntimePort):
    def __init__(
        self,
        *,
        fail_member: str | None = None,
        failed_result_member: str | None = None,
        cancelled_member: str | None = None,
        corrupt_member: str | None = None,
        corrupt_checker: bool = False,
        raw_error: str = "provider leaked secret text",
    ) -> None:
        self.fail_member = fail_member
        self.failed_result_member = failed_result_member
        self.cancelled_member = cancelled_member
        self.corrupt_member = corrupt_member
        self.corrupt_checker = corrupt_checker
        self.raw_error = raw_error
        self.requests: list[RuntimeRequest] = []
        self.active = 0
        self.max_active = 0

    @property
    def runtime_id(self) -> str:
        return "v01-three-agent-scenario-test"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset(
            {
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.DURABLE_RESUME,
                RuntimeCapability.PARALLELISM,
                RuntimeCapability.SUBAGENTS,
            }
        )

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        return f"resume:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        member_id = str(request.payload["member_id"])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01 if member_id.startswith("worker-") else 0)
            if member_id == self.fail_member:
                raise RuntimeError("isolated worker failure")
            if member_id == self.failed_result_member:
                return RuntimeResult(
                    outcome=RuntimeOutcome.FAILED,
                    error=self.raw_error,
                    error_code=RuntimeErrorCode.INTERNAL,
                )
            if member_id == self.cancelled_member:
                return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
            if member_id == "checker":
                return self._checker_result(request)
            return self._worker_result(request)
        finally:
            self.active -= 1

    def _worker_result(self, request: RuntimeRequest) -> RuntimeResult:
        handoff = cast(Mapping[str, object], request.payload["handoff"])
        assignment = SourceInspectionAssignment.from_payload(
            cast(Mapping[str, object], handoff["source_assignment"])
        )
        assert request.task_id != assignment.task_id
        assert request.task_id != assignment.effect_id
        text = Path(assignment.source.locator).read_text(encoding="utf-8")
        output = encode_source_result(assignment, _result_set(assignment, text=text))
        if assignment.member_id == self.corrupt_member:
            result_set = cast(dict[str, object], output["result_set"])
            items = cast(list[dict[str, object]], result_set["items"])
            items[0]["snippet"] = "tampered after digest"
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output=output)

    def _checker_result(self, request: RuntimeRequest) -> RuntimeResult:
        handoff = cast(Mapping[str, object], request.payload["handoff"])
        assignments = tuple(
            SourceInspectionAssignment.from_payload(cast(Mapping[str, object], item))
            for item in cast(list[object], handoff["source_assignments"])
        )
        inbound = tuple(
            _handoff_from_payload(cast(Mapping[str, object], item))
            for item in cast(list[object], request.payload["inbound_handoffs"])
        )
        summary = V01CheckerAgent().compare(
            team_id=str(request.payload["team_id"]),
            task_id=str(handoff["shared_task_id"]),
            checker_id=str(request.payload["member_id"]),
            assignments=assignments,
            handoffs=inbound,
        ).to_payload()
        if self.corrupt_checker:
            summary["task_id"] = "other-task"
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"checker_summary": summary},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError(f"terminal member must not resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


class SimulatedProcessCrash(RuntimeError):
    pass


def _handoff_from_payload(value: Mapping[str, object]) -> AgentHandoff:
    return AgentHandoff(
        handoff_id=str(value["handoff_id"]),
        team_id=str(value["team_id"]),
        sender_id=str(value["sender_id"]),
        recipient_id=str(value["recipient_id"]),
        kind=HandoffKind(str(value["kind"])),
        correlation_id=str(value["correlation_id"]),
        payload=cast(dict[str, object], value["payload"]),
    )


def _result_set(
    assignment: SourceInspectionAssignment,
    *,
    text: str,
) -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id=f"result:{assignment.assignment_id}",
        workspace_id=assignment.source.workspace_id,
        query="compare declared condition",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id=f"doc:{assignment.source.source_id}",
                title="controlled source",
                snippet=text,
                rank=1.0,
                why_matched="deterministic declared-source fixture",
                evidence=(
                    ResearchEvidence(
                        source_id=assignment.source.source_id,
                        source_kind=assignment.source.kind,
                        locator=assignment.source.locator,
                        observed_at="2026-09-03T00:00:00+00:00",
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
        created_at="2026-09-03T00:00:01+00:00",
    )


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(
            ToolSpec("file.read", "Read declared source", ToolRisk.READ_ONLY),
            ToolSpec("web.read", "Read web source", ToolRisk.READ_ONLY),
        ),
        model_profiles={"test"},
    )


def _definition(agent_id: str, grants: tuple[ToolGrant, ...]) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=1,
        name=agent_id,
        goal="Complete one bounded V0.1 Scenario A role.",
        instructions="Use only declared evidence and return canonical structured output.",
        model_profile="test",
        tool_grants=grants,
        enabled=True,
    )


def _config(
    source_a: Path,
    source_b: Path,
    *,
    checker_grants: tuple[ToolGrant, ...] | None = None,
    worker_a_grants: tuple[ToolGrant, ...] | None = None,
) -> V01ThreeAgentConfig:
    grant = ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",))
    return V01ThreeAgentConfig(
        checker=V01ChildAssignment(
            member_id="checker",
            agent_id="checker-agent",
            agent_version=1,
            requested_grants=(grant,) if checker_grants is None else checker_grants,
            instruction="Compare exactly two canonical source results and return the typed summary.",
        ),
        workers=(
            V01SourceWorkerAssignment(
                member_id="worker-a",
                agent_id="worker-a-agent",
                agent_version=1,
                requested_grants=(grant,) if worker_a_grants is None else worker_a_grants,
                instruction="Inspect only declared source A.",
                source=SourceSpec(
                    source_id="source-a",
                    workspace_id="workspace",
                    kind=SourceKind.LOCAL_FILE,
                    locator=str(source_a),
                ),
                max_items=1,
            ),
            V01SourceWorkerAssignment(
                member_id="worker-b",
                agent_id="worker-b-agent",
                agent_version=1,
                requested_grants=(grant,),
                instruction="Inspect only declared source B.",
                source=SourceSpec(
                    source_id="source-b",
                    workspace_id="workspace",
                    kind=SourceKind.LOCAL_FILE,
                    locator=str(source_b),
                ),
                max_items=1,
            ),
        ),
    )


def _initialize(db_path: Path) -> None:
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    definitions = AgentDefinitionRepository(sqlite)
    compiler = _compiler()
    grant = ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",))
    for agent_id in ("checker-agent", "worker-a-agent", "worker-b-agent"):
        definition = _definition(agent_id, (grant,))
        definitions.save_draft(compiler.compile(definition))
        definitions.activate(definition)


def _open(
    db_path: Path,
    source_a: Path,
    source_b: Path,
    *,
    runtime: RecordingScenarioRuntime | None = None,
    config: V01ThreeAgentConfig | None = None,
) -> tuple[RecordingScenarioRuntime, MultiAgentStore, MultiAgentSupervisor, V01ThreeAgentSupervisor]:
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    definitions = AgentDefinitionRepository(sqlite)
    active_runtime = runtime or RecordingScenarioRuntime()
    coordinator = MultiAgentSupervisor(
        runtime=active_runtime,
        store=store,
        definitions=definitions,
    )
    adapter = V01ThreeAgentSupervisor(
        coordinator=coordinator,
        store=store,
        definitions=definitions,
        config=config or _config(source_a, source_b),
    )
    return active_runtime, store, coordinator, adapter


def _fixture(tmp_path: Path, *, same: bool = False) -> tuple[Path, Path, Path]:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    source_a.write_text("same evidence" if same else "alpha evidence", encoding="utf-8")
    source_b.write_text("same evidence" if same else "beta evidence", encoding="utf-8")
    db_path = tmp_path / "nika.db"
    _initialize(db_path)
    return db_path, source_a, source_b


def test_two_source_workers_and_checker_reach_one_terminal_result(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path, same=True)
    runtime, store, _, adapter = _open(db_path, source_a, source_b)

    result = asyncio.run(
        adapter.run(
            user_goal="Compare two declared sources.",
            shared_task_id="task-v01-a",
            team_id="team-v01-a",
        )
    )

    assert [member.member_id for member in store.members("team-v01-a")] == [
        "checker",
        "worker-a",
        "worker-b",
    ]
    assert store.quota("team-v01-a") == TeamQuota(
        max_depth=1,
        max_children_per_parent=2,
        max_total_agents=3,
        max_parallel=2,
    )
    assert [request.payload["member_id"] for request in runtime.requests] == [
        "worker-a",
        "worker-b",
        "checker",
    ]
    assert runtime.max_active == 2
    assert all(request.timeout_seconds == 300.0 for request in runtime.requests)
    worker_effects: set[str] = set()
    for request in runtime.requests[:2]:
        handoff = cast(dict[str, object], request.payload["handoff"])
        assignment = SourceInspectionAssignment.from_payload(
            cast(Mapping[str, object], handoff["source_assignment"])
        )
        assert assignment.team_id == "team-v01-a"
        assert assignment.task_id == "task-v01-a"
        assert assignment.effect_id != request.task_id
        worker_effects.add(assignment.effect_id)
    assert len(worker_effects) == 2
    checker_request = runtime.requests[2]
    assert checker_request.payload["parent_id"] is None
    assert len(cast(list[object], checker_request.payload["inbound_handoffs"])) == 2

    assert result.team_state is TeamState.COMPLETED
    assert [worker.state for worker in result.workers] == [
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]
    assert result.checker.state is MemberState.COMPLETED
    assert result.final_output["status"] == CheckerStatus.AGREE.value
    assert result.final_output["checker_output_validated"] is True
    summary = cast(dict[str, object], result.final_output["checker_summary"])
    assert summary["schema"] == "nika.v01.checker-summary:v2"
    assert summary["task_id"] == "task-v01-a"


def test_worker_failure_is_isolated_and_checker_returns_typed_degraded_result(
    tmp_path: Path,
) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    runtime = RecordingScenarioRuntime(fail_member="worker-a")
    runtime, store, _, adapter = _open(
        db_path,
        source_a,
        source_b,
        runtime=runtime,
    )

    result = asyncio.run(
        adapter.run(
            user_goal="Compare despite one isolated source failure.",
            shared_task_id="task-failure",
            team_id="team-failure",
        )
    )

    assert store.member("team-failure", "worker-a").state is MemberState.FAILED
    assert store.member("team-failure", "worker-b").state is MemberState.COMPLETED
    assert store.member("team-failure", "checker").state is MemberState.COMPLETED
    assert [request.payload["member_id"] for request in runtime.requests] == [
        "worker-a",
        "worker-b",
        "checker",
    ]
    assert result.final_output["status"] == CheckerStatus.WORKER_ERROR.value
    summary = cast(dict[str, object], result.final_output["checker_summary"])
    sources = cast(list[dict[str, object]], summary["sources"])
    assert sources[0]["state"] == "worker_error"
    assert "result_set" not in sources[0]
    assert sources[1]["state"] == "valid"


def test_worker_cancellation_is_isolated_and_reported_as_worker_error(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    runtime = RecordingScenarioRuntime(cancelled_member="worker-a")
    _, store, _, adapter = _open(db_path, source_a, source_b, runtime=runtime)

    result = asyncio.run(
        adapter.run(
            user_goal="Report one cancelled source worker.",
            shared_task_id="task-cancelled-worker",
            team_id="team-cancelled-worker",
        )
    )

    assert store.member("team-cancelled-worker", "worker-a").state is MemberState.CANCELLED
    assert store.member("team-cancelled-worker", "worker-b").state is MemberState.COMPLETED
    assert result.team_state is TeamState.COMPLETED
    assert result.final_output["status"] == CheckerStatus.WORKER_ERROR.value


def test_checker_failure_makes_team_failed_and_never_claims_validated_comparison(
    tmp_path: Path,
) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    runtime = RecordingScenarioRuntime(fail_member="checker")
    _, store, _, adapter = _open(db_path, source_a, source_b, runtime=runtime)

    result = asyncio.run(
        adapter.run(
            user_goal="Fail closed when the checker fails.",
            shared_task_id="task-checker-failure",
            team_id="team-checker-failure",
        )
    )

    assert store.member("team-checker-failure", "checker").state is MemberState.FAILED
    assert result.team_state is TeamState.FAILED
    assert result.final_output["status"] == CheckerStatus.EVIDENCE_INVALID.value
    assert result.final_output["checker_output_validated"] is False


def test_tampered_worker_evidence_fails_closed_without_synthetic_result(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    runtime = RecordingScenarioRuntime(corrupt_member="worker-b")
    _, _, _, adapter = _open(db_path, source_a, source_b, runtime=runtime)

    result = asyncio.run(
        adapter.run(
            user_goal="Reject modified evidence.",
            shared_task_id="task-tamper",
            team_id="team-tamper",
        )
    )

    assert result.final_output["status"] == CheckerStatus.EVIDENCE_INVALID.value
    summary = cast(dict[str, object], result.final_output["checker_summary"])
    sources = cast(list[dict[str, object]], summary["sources"])
    invalid = next(item for item in sources if item["worker_id"] == "worker-b")
    assert invalid["state"] == "evidence_invalid"
    assert "result_set" not in invalid


def test_checker_runtime_output_must_match_deterministic_summary(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path, same=True)
    runtime = RecordingScenarioRuntime(corrupt_checker=True)
    _, _, _, adapter = _open(db_path, source_a, source_b, runtime=runtime)

    result = asyncio.run(
        adapter.run(
            user_goal="Verify checker output.",
            shared_task_id="task-checker-bind",
            team_id="team-checker-bind",
        )
    )

    assert result.final_output["status"] == CheckerStatus.EVIDENCE_INVALID.value
    assert result.final_output["checker_output_validated"] is False


def test_restart_after_workers_runs_only_missing_checker(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    first_runtime, first_store, first_coordinator, first_adapter = _open(
        db_path,
        source_a,
        source_b,
    )

    async def crash_before_checker(**kwargs: object):
        del kwargs
        raise SimulatedProcessCrash("after workers before checker")

    first_coordinator.run_root_member = crash_before_checker  # type: ignore[method-assign]
    with pytest.raises(SimulatedProcessCrash, match="after workers"):
        asyncio.run(
            first_adapter.run(
                user_goal="Resume the exact durable comparison.",
                shared_task_id="task-restart-workers",
                team_id="team-restart-workers",
            )
        )

    assert [request.payload["member_id"] for request in first_runtime.requests] == [
        "worker-a",
        "worker-b",
    ]
    assert [member.state for member in first_store.members("team-restart-workers")] == [
        MemberState.SPAWNED,
        MemberState.COMPLETED,
        MemberState.COMPLETED,
    ]

    restarted_runtime, _, _, restarted_adapter = _open(db_path, source_a, source_b)
    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Resume the exact durable comparison.",
            shared_task_id="task-restart-workers",
            team_id="team-restart-workers",
        )
    )

    assert [request.payload["member_id"] for request in restarted_runtime.requests] == [
        "checker"
    ]
    assert result.team_state is TeamState.COMPLETED
    assert result.final_output["checker_output_validated"] is True


def test_restart_after_checker_reconstructs_identical_result_without_runtime(
    tmp_path: Path,
) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    first_runtime, first_store, first_coordinator, first_adapter = _open(
        db_path,
        source_a,
        source_b,
    )

    def crash_before_finalize(team_id: str) -> TeamState:
        assert team_id == "team-restart-finalize"
        raise SimulatedProcessCrash("after checker before finalize")

    first_coordinator.finalize_team = crash_before_finalize  # type: ignore[method-assign]
    with pytest.raises(SimulatedProcessCrash, match="before finalize"):
        asyncio.run(
            first_adapter.run(
                user_goal="Reconstruct one exact result.",
                shared_task_id="task-restart-finalize",
                team_id="team-restart-finalize",
            )
        )
    assert len(first_runtime.requests) == 3
    assert first_store.team_state("team-restart-finalize") is TeamState.ACTIVE

    restarted_runtime, _, _, restarted_adapter = _open(db_path, source_a, source_b)
    result = asyncio.run(
        restarted_adapter.run(
            user_goal="Reconstruct one exact result.",
            shared_task_id="task-restart-finalize",
            team_id="team-restart-finalize",
        )
    )

    assert restarted_runtime.requests == []
    assert result.team_state is TeamState.COMPLETED
    assert result.final_output["checker_output_validated"] is True


def test_restart_rejects_changed_logical_task_identity(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    _, _, _, adapter = _open(db_path, source_a, source_b)
    asyncio.run(
        adapter.run(
            user_goal="Keep exact task identity.",
            shared_task_id="task-original",
            team_id="team-identity",
        )
    )

    _, _, _, restarted = _open(db_path, source_a, source_b)
    with pytest.raises(PermissionError, match="task identity"):
        asyncio.run(
            restarted.run(
                user_goal="Keep exact task identity.",
                shared_task_id="task-substituted",
                team_id="team-identity",
            )
        )


def test_runtime_error_text_is_bounded_before_storage_and_projection(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    secret = "api-key=must-not-persist"
    runtime = RecordingScenarioRuntime(
        failed_result_member="worker-a",
        raw_error=secret,
    )
    _, store, _, adapter = _open(db_path, source_a, source_b, runtime=runtime)

    result = asyncio.run(
        adapter.run(
            user_goal="Bound provider failure output.",
            shared_task_id="task-error",
            team_id="team-error",
        )
    )

    persisted = store.member_result("team-error", "worker-a")
    assert persisted.error == RuntimeErrorCode.INTERNAL.value
    assert secret not in str(result.final_output)


def test_permission_expansion_fails_before_any_runtime_execution(tmp_path: Path) -> None:
    db_path, source_a, source_b = _fixture(tmp_path)
    web_grant = ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",))
    config = _config(source_a, source_b, worker_a_grants=(web_grant,))
    runtime, store, _, adapter = _open(
        db_path,
        source_a,
        source_b,
        config=config,
    )

    with pytest.raises(PermissionError, match="activated definition|ungranted tool"):
        asyncio.run(
            adapter.run(
                user_goal="Do not widen permissions.",
                shared_task_id="task-permission",
                team_id="team-permission",
            )
        )
    assert runtime.requests == []
    with pytest.raises(KeyError):
        store.team_state("team-permission")
