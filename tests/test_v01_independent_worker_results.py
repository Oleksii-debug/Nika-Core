from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    ChildRequest,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
)
from nika_core.multi_agent.research_results import (
    SourceInspectionAssignment,
    SourceResultBindingError,
    decode_source_result,
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
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.tools import ToolRisk, ToolSpec


class FixtureInspectionRuntime(AgentRuntimePort):
    def __init__(self, *, fail_member: str | None = None) -> None:
        self.fail_member = fail_member
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, str, str]] = []

    @property
    def runtime_id(self) -> str:
        return "fixture-source-inspection"

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
        return f"resume:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        handoff = cast(Mapping[str, object], request.payload["handoff"])
        assignment = SourceInspectionAssignment.from_payload(handoff)
        if request.task_id != assignment.effect_id:
            raise AssertionError("runtime task identity must match assignment effect identity")

        self.calls.append(
            (assignment.member_id, assignment.tool_call_id, assignment.effect_id)
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if assignment.member_id == self.fail_member:
                raise RuntimeError("deterministic fixture worker failure")
            text = Path(assignment.source.locator).read_text(encoding="utf-8")
            result_set = _result_set(assignment, text=text)
            return RuntimeResult(
                outcome=RuntimeOutcome.COMPLETED,
                output=encode_source_result(assignment, result_set),
            )
        finally:
            self.active -= 1

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError(f"terminal worker must not resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


class NoRunRuntime(FixtureInspectionRuntime):
    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError(f"terminal worker must not rerun: {request.task_id}")


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(ToolSpec("file.read", "Read declared source", ToolRisk.READ_ONLY),),
        model_profiles={"test"},
    )


def _definition(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        version=1,
        name=agent_id,
        goal="Inspect exactly one declared deterministic source.",
        instructions="Return only structured provenance for the bounded assignment.",
        model_profile="test",
        tool_grants=(
            ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
        ),
        enabled=True,
    )


def _definitions(sqlite: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(sqlite)
    compiler = _compiler()
    for agent_id in ("supervisor", "worker-a", "worker-b"):
        definition = _definition(agent_id)
        repository.save_draft(compiler.compile(definition))
        repository.activate(definition)
    return repository


def _store(tmp_path: Path) -> tuple[SQLiteStore, MultiAgentStore]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-v01",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(
            ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
        ),
        quota=TeamQuota(
            max_depth=1,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    return sqlite, store


def _assignment(
    *,
    member_id: str,
    source_id: str,
    locator: str,
) -> SourceInspectionAssignment:
    return SourceInspectionAssignment(
        assignment_id=f"assignment:{member_id}",
        member_id=member_id,
        source=SourceSpec(
            source_id=source_id,
            workspace_id="fixture-workspace",
            kind=SourceKind.LOCAL_FILE,
            locator=locator,
        ),
        tool_call_id=f"tool-call:{member_id}",
        effect_id=f"team:team-v01:{member_id}",
        max_items=1,
    )


def _result_set(
    assignment: SourceInspectionAssignment,
    *,
    text: str,
) -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id=f"result:{assignment.member_id}",
        workspace_id=assignment.source.workspace_id,
        query=f"inspect:{assignment.source.source_id}",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id=f"doc:{assignment.source.source_id}",
                title=Path(assignment.source.locator).name,
                snippet=text,
                rank=1.0,
                why_matched="deterministic fixture source",
                evidence=(
                    ResearchEvidence(
                        source_id=assignment.source.source_id,
                        source_kind=assignment.source.kind,
                        locator=assignment.source.locator,
                        observed_at="2026-08-27T00:00:00+00:00",
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
        created_at="2026-08-27T00:00:01+00:00",
    )


def _request(assignment: SourceInspectionAssignment) -> ChildRequest:
    return ChildRequest(
        member_id=assignment.member_id,
        agent_id=assignment.member_id,
        agent_version=1,
        thread_id=f"thread:{assignment.member_id}",
        requested_grants=(
            ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",)),
        ),
        payload=assignment.to_payload(),
    )


def test_independent_workers_retain_result_identity_and_restart_state(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    source_a.write_text("alpha fixture", encoding="utf-8")
    source_b.write_text("beta fixture", encoding="utf-8")
    assignment_a = _assignment(
        member_id="worker-a",
        source_id="source-a",
        locator=str(source_a),
    )
    assignment_b = _assignment(
        member_id="worker-b",
        source_id="source-b",
        locator=str(source_b),
    )

    sqlite, store = _store(tmp_path)
    runtime = FixtureInspectionRuntime(fail_member="worker-a")
    supervisor = MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=_definitions(sqlite),
    )

    executions = asyncio.run(
        supervisor.fan_out(
            team_id="team-v01",
            parent_id="root",
            requests=(_request(assignment_a), _request(assignment_b)),
        )
    )
    by_member = {execution.member.member_id: execution for execution in executions}

    assert runtime.max_active == 2
    assert len({call[1] for call in runtime.calls}) == 2
    assert len({call[2] for call in runtime.calls}) == 2
    assert by_member["worker-a"].exception == "RuntimeError"
    assert by_member["worker-a"].result is None
    assert by_member["worker-b"].exception is None
    assert by_member["worker-b"].result is not None
    assert store.member("team-v01", "worker-a").state is MemberState.FAILED
    assert store.member("team-v01", "worker-b").state is MemberState.COMPLETED

    persisted_a = SourceInspectionAssignment.from_payload(
        store.task_payload("team-v01", "worker-a")
    )
    persisted_b = SourceInspectionAssignment.from_payload(
        store.task_payload("team-v01", "worker-b")
    )
    assert persisted_a == assignment_a
    assert persisted_b == assignment_b
    assert persisted_a.source.source_id != persisted_b.source.source_id
    assert persisted_a.max_items == persisted_b.max_items == 1
    assert tuple(
        grant.tool_id for grant in store.member("team-v01", "worker-b").tool_grants
    ) == ("file.read",)

    worker_b_result = by_member["worker-b"].result
    assert worker_b_result is not None
    checked = decode_source_result(
        assignment_b,
        member_id="worker-b",
        output=worker_b_result.output,
    )
    assert checked.result_set_id == "result:worker-b"
    assert checked.items[0].snippet == "beta fixture"
    assert checked.items[0].evidence[0].source_id == "source-b"

    with sqlite.connection() as conn:
        persisted_row = conn.execute(
            "SELECT outcome, payload_json, error FROM multi_agent_results "
            "WHERE team_id = ? AND member_id = ? ORDER BY created_at DESC LIMIT 1",
            ("team-v01", "worker-b"),
        ).fetchone()
    assert persisted_row is not None
    assert persisted_row["outcome"] == RuntimeOutcome.COMPLETED.value
    assert persisted_row["error"] is None
    persisted_output = json.loads(persisted_row["payload_json"])
    persisted_checked = decode_source_result(
        assignment_b,
        member_id="worker-b",
        output=persisted_output,
    )
    assert persisted_checked.items[0].evidence[0].locator == str(source_b)

    restarted_sqlite = SQLiteStore(sqlite.path)
    restarted_store = MultiAgentStore(restarted_sqlite)
    assert restarted_store.member("team-v01", "worker-a").state is MemberState.FAILED
    assert restarted_store.member("team-v01", "worker-b").state is MemberState.COMPLETED
    restarted_supervisor = MultiAgentSupervisor(
        runtime=NoRunRuntime(),
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_sqlite),
    )
    assert asyncio.run(restarted_supervisor.recover_team("team-v01")) == ()


def test_checker_rejects_cross_target_and_tampered_provenance(tmp_path: Path) -> None:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    source_a.write_text("alpha fixture", encoding="utf-8")
    source_b.write_text("beta fixture", encoding="utf-8")
    assignment_a = _assignment(
        member_id="worker-a",
        source_id="source-a",
        locator=str(source_a),
    )
    assignment_b = _assignment(
        member_id="worker-b",
        source_id="source-b",
        locator=str(source_b),
    )

    output_b = encode_source_result(
        assignment_b,
        _result_set(assignment_b, text="beta fixture"),
    )
    with pytest.raises(SourceResultBindingError, match="assignment_id"):
        decode_source_result(
            assignment_a,
            member_id="worker-a",
            output=output_b,
        )

    output_a = encode_source_result(
        assignment_a,
        _result_set(assignment_a, text="alpha fixture"),
    )
    tampered = cast(dict[str, object], json.loads(json.dumps(output_a)))
    result_set = cast(dict[str, object], tampered["result_set"])
    items = cast(list[dict[str, object]], result_set["items"])
    evidence = cast(list[dict[str, object]], items[0]["evidence"])
    evidence[0]["source_id"] = "source-b"

    with pytest.raises(SourceResultBindingError, match="evidence source_id"):
        decode_source_result(
            assignment_a,
            member_id="worker-a",
            output=tampered,
        )


def test_result_cannot_exceed_declared_assignment_bound(tmp_path: Path) -> None:
    source = tmp_path / "source-a.txt"
    source.write_text("alpha fixture", encoding="utf-8")
    assignment = _assignment(
        member_id="worker-a",
        source_id="source-a",
        locator=str(source),
    )
    one_item = _result_set(assignment, text="alpha fixture").items[0]
    over_limit = ResearchResultSet(
        result_set_id="result:over-limit",
        workspace_id=assignment.source.workspace_id,
        query="inspect:source-a",
        items=(
            one_item,
            ResearchResultItem(
                ordinal=1,
                document_id="doc:source-a:second",
                title="second",
                snippet="second",
                rank=0.5,
                why_matched="deterministic fixture source",
                evidence=one_item.evidence,
            ),
        ),
        created_at="2026-08-27T00:00:02+00:00",
    )

    with pytest.raises(SourceResultBindingError, match="max_items"):
        encode_source_result(assignment, over_limit)
