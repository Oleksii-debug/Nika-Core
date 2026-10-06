from __future__ import annotations

from copy import deepcopy

import pytest

from nika_core.multi_agent.checker import CheckerStatus, V01CheckerAgent
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind
from nika_core.multi_agent.research_results import (
    SourceInspectionAssignment,
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


def _assignment(
    worker: str,
    source: str,
    *,
    team_id: str = "team-v01",
    task_id: str = "task-v01",
) -> SourceInspectionAssignment:
    return SourceInspectionAssignment(
        team_id=team_id,
        task_id=task_id,
        assignment_id=f"assignment:{worker}",
        member_id=worker,
        source=SourceSpec(
            source_id=source,
            workspace_id="workspace",
            kind=SourceKind.LOCAL_FILE,
            locator=f"fixture://{source}",
        ),
        tool_call_id=f"tool:{worker}",
        effect_id=f"effect:{worker}",
        max_items=5,
    )


def _assignments() -> tuple[SourceInspectionAssignment, SourceInspectionAssignment]:
    return _assignment("worker-a", "source-a"), _assignment("worker-b", "source-b")


def _result(
    assignment: SourceInspectionAssignment,
    contents: tuple[str, ...],
) -> dict[str, object]:
    return encode_source_result(
        assignment,
        ResearchResultSet(
            result_set_id=f"result:{assignment.member_id}",
            workspace_id=assignment.source.workspace_id,
            query="shared declared query",
            created_at="2026-09-03T00:00:00+00:00",
            items=tuple(
                ResearchResultItem(
                    ordinal=index,
                    document_id=f"{assignment.source.source_id}:doc:{index}",
                    title="matching title",
                    snippet=content,
                    rank=1.0,
                    why_matched="declared comparator fixture",
                    evidence=(
                        ResearchEvidence(
                            source_id=assignment.source.source_id,
                            source_kind=assignment.source.kind,
                            locator=assignment.source.locator,
                            observed_at="2026-09-03T00:00:00+00:00",
                            freshness=FreshnessState.CURRENT,
                        ),
                    ),
                )
                for index, content in enumerate(contents)
            ),
        ),
    )


def _handoff(
    assignment: SourceInspectionAssignment,
    *,
    contents: tuple[str, ...] = ("same",),
    kind: HandoffKind = HandoffKind.RESULT,
    payload: dict[str, object] | None = None,
    team_id: str | None = None,
    recipient_id: str = "checker",
    correlation_id: str | None = None,
) -> AgentHandoff:
    effective_team = team_id or assignment.team_id
    effective_payload = (
        _result(assignment, contents)
        if payload is None and kind is HandoffKind.RESULT
        else payload or {"error": "RuntimeError"}
    )
    return AgentHandoff(
        team_id=effective_team,
        sender_id=assignment.member_id,
        recipient_id=recipient_id,
        kind=kind,
        payload=effective_payload,
        handoff_id=f"handoff:{assignment.member_id}",
        correlation_id=(
            correlation_id
            or f"team:{effective_team}:{recipient_id}:{assignment.member_id}"
        ),
    )


def _compare(handoffs: tuple[AgentHandoff, ...]):
    return V01CheckerAgent().compare(
        team_id="team-v01",
        task_id="task-v01",
        checker_id="checker",
        assignments=_assignments(),
        handoffs=handoffs,
    )


def test_checker_reports_agree_for_equal_canonical_content_and_preserves_provenance() -> None:
    assignment_a, assignment_b = _assignments()
    summary = _compare((_handoff(assignment_a), _handoff(assignment_b)))

    assert summary.status is CheckerStatus.AGREE
    assert summary.agreements == ("result_set",)
    assert summary.differences == ()
    payload = summary.to_payload()
    assert payload["schema"] == "nika.v01.checker-summary:v2"
    assert payload["team_id"] == "team-v01"
    assert payload["task_id"] == "task-v01"
    assert payload["comparison_domain"] == "canonical-research-items:v1"
    sources = payload["sources"]
    assert [item["source_id"] for item in sources] == ["source-a", "source-b"]
    assert all(item["result_digest"] for item in sources)


def test_checker_reports_disagree_when_no_canonical_item_matches() -> None:
    assignment_a, assignment_b = _assignments()
    summary = _compare(
        (
            _handoff(assignment_a, contents=("alpha",)),
            _handoff(assignment_b, contents=("beta",)),
        )
    )

    assert summary.status is CheckerStatus.DISAGREE
    assert summary.agreements == ()
    assert len(summary.differences) == 1
    difference = summary.differences[0].to_payload()
    assert difference["field"] == "result_set"
    assert [item["source_id"] for item in difference["values"]] == [
        "source-a",
        "source-b",
    ]


def test_checker_reports_partial_only_for_mixed_shared_and_distinct_items() -> None:
    assignment_a, assignment_b = _assignments()
    summary = _compare(
        (
            _handoff(assignment_a, contents=("shared", "only-a")),
            _handoff(assignment_b, contents=("shared", "only-b")),
        )
    )

    assert summary.status is CheckerStatus.PARTIAL
    assert len(summary.agreements) == 1
    assert summary.agreements[0].startswith("item:")
    assert len(summary.differences) == 1


def test_missing_input_has_typed_state_and_never_invents_result() -> None:
    assignment_a, _ = _assignments()
    summary = _compare((_handoff(assignment_a),))

    assert summary.status is CheckerStatus.MISSING
    assert summary.agreements == ()
    assert summary.differences == ()
    missing = summary.to_payload()["sources"][1]
    assert missing["state"] == "missing"
    assert "result_set" not in missing
    assert "result_digest" not in missing


def test_worker_error_takes_precedence_over_missing_and_has_no_synthetic_result() -> None:
    assignment_a, assignment_b = _assignments()
    summary = _compare(
        (
            _handoff(
                assignment_a,
                kind=HandoffKind.ERROR,
                payload={"error": "RuntimeError"},
            ),
        )
    )

    assert summary.status is CheckerStatus.WORKER_ERROR
    sources = summary.to_payload()["sources"]
    assert sources[0]["state"] == "worker_error"
    assert sources[1]["state"] == "missing"
    assert all("result_set" not in source for source in sources)
    assert assignment_b.member_id == sources[1]["worker_id"]


@pytest.mark.parametrize(
    "bad_handoff",
    (
        lambda assignment: _handoff(assignment, team_id="team-other"),
        lambda assignment: _handoff(assignment, recipient_id="other-checker"),
        lambda assignment: _handoff(assignment, correlation_id="wrong-correlation"),
        lambda assignment: AgentHandoff(
            team_id="team-v01",
            sender_id="worker-unknown",
            recipient_id="checker",
            kind=HandoffKind.RESULT,
            payload={},
            correlation_id="team:team-v01:checker:worker-unknown",
        ),
    ),
)
def test_wrong_handoff_identity_returns_evidence_invalid_instead_of_throwing(
    bad_handoff,
) -> None:
    assignment_a, _ = _assignments()
    summary = _compare((bad_handoff(assignment_a),))

    assert summary.status is CheckerStatus.EVIDENCE_INVALID
    assert summary.agreements == ()
    assert summary.differences == ()
    assert all("result_set" not in item for item in summary.to_payload()["sources"])


def test_duplicate_worker_output_is_evidence_invalid() -> None:
    assignment_a, _ = _assignments()
    handoff = _handoff(assignment_a)
    summary = _compare((handoff, handoff))

    assert summary.status is CheckerStatus.EVIDENCE_INVALID
    invalid = summary.to_payload()["sources"][0]
    assert invalid["state"] == "evidence_invalid"
    assert "result_set" not in invalid


def test_tampered_digest_and_cross_task_replay_are_evidence_invalid() -> None:
    assignment_a, assignment_b = _assignments()
    tampered = deepcopy(_result(assignment_a, ("alpha",)))
    tampered["result_digest"] = "0" * 64
    other_task_assignment = _assignment(
        "worker-b",
        "source-b",
        task_id="task-other",
    )
    summary = _compare(
        (
            _handoff(assignment_a, payload=tampered),
            _handoff(
                assignment_b,
                payload=_result(other_task_assignment, ("beta",)),
            ),
        )
    )

    assert summary.status is CheckerStatus.EVIDENCE_INVALID
    assert all(
        item["state"] == "evidence_invalid"
        for item in summary.to_payload()["sources"]
    )


def test_checker_requires_exactly_two_distinct_trusted_assignments() -> None:
    assignment_a, _ = _assignments()
    with pytest.raises(ValueError, match="exactly two"):
        V01CheckerAgent().compare(
            team_id="team-v01",
            task_id="task-v01",
            checker_id="checker",
            assignments=(assignment_a,),
            handoffs=(),
        )
    with pytest.raises(ValueError, match="source identities"):
        V01CheckerAgent().compare(
            team_id="team-v01",
            task_id="task-v01",
            checker_id="checker",
            assignments=(
                assignment_a,
                _assignment("worker-b", "source-a"),
            ),
            handoffs=(),
        )
