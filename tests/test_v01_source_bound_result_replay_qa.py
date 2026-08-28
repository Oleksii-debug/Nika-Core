from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields, replace

import pytest

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

_TEAM_A = "team-a"
_TASK_A = "task-a"


def _assignment(
    *,
    member_id: str = "worker-a",
    source_id: str = "source-a",
    workspace_id: str = "workspace-a",
    locator: str = "fixture://source-a",
    tool_call_id: str = "tool-call-a",
    effect_id: str = "effect-a",
    assignment_id: str = "assignment-a",
) -> SourceInspectionAssignment:
    kwargs: dict[str, object] = {
        "assignment_id": assignment_id,
        "member_id": member_id,
        "source": SourceSpec(
            source_id=source_id,
            workspace_id=workspace_id,
            kind=SourceKind.LOCAL_FILE,
            locator=locator,
        ),
        "tool_call_id": tool_call_id,
        "effect_id": effect_id,
        "max_items": 1,
    }
    field_names = {item.name for item in fields(SourceInspectionAssignment)}
    if "team_id" in field_names:
        kwargs["team_id"] = _TEAM_A
    if "task_id" in field_names:
        kwargs["task_id"] = _TASK_A
    return SourceInspectionAssignment(**kwargs)  # type: ignore[arg-type]


def _result_set(
    assignment: SourceInspectionAssignment,
    *,
    result_set_id: str = "result-a",
    snippet: str = "alpha",
) -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id=result_set_id,
        workspace_id=assignment.source.workspace_id,
        query=f"inspect:{assignment.source.source_id}",
        created_at="2026-08-28T00:00:00+00:00",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id=f"doc:{assignment.source.source_id}",
                title="fixture",
                snippet=snippet,
                rank=1.0,
                why_matched="deterministic QA fixture",
                evidence=(
                    ResearchEvidence(
                        source_id=assignment.source.source_id,
                        source_kind=assignment.source.kind,
                        locator=assignment.source.locator,
                        observed_at="2026-08-28T00:00:00+00:00",
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
    )


def _decode(
    assignment: SourceInspectionAssignment,
    output: dict[str, object],
    *,
    member_id: str | None = None,
) -> ResearchResultSet:
    return decode_source_result(
        assignment,
        member_id=member_id or assignment.member_id,
        output=output,
    )


def test_current_worker_source_workspace_tool_and_effect_substitution_fail_closed() -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))

    worker_b = _assignment(member_id="worker-b", assignment_id="assignment-b")
    with pytest.raises(SourceResultBindingError):
        _decode(worker_b, output, member_id="worker-b")

    source_b = _assignment(
        source_id="source-b",
        locator="fixture://source-b",
        assignment_id=assignment.assignment_id,
    )
    with pytest.raises(SourceResultBindingError, match="source_id|locator"):
        _decode(source_b, output)

    workspace_b = _assignment(
        workspace_id="workspace-b",
        assignment_id=assignment.assignment_id,
    )
    with pytest.raises(SourceResultBindingError, match="workspace_id"):
        _decode(workspace_b, output)

    wrong_tool = _assignment(
        tool_call_id="tool-call-b",
        assignment_id=assignment.assignment_id,
    )
    with pytest.raises(SourceResultBindingError, match="tool_call_id"):
        _decode(wrong_tool, output)

    wrong_effect = _assignment(
        effect_id="effect-b",
        assignment_id=assignment.assignment_id,
    )
    with pytest.raises(SourceResultBindingError, match="effect_id"):
        _decode(wrong_effect, output)


def test_source_result_contract_carries_explicit_team_and_task_identity() -> None:
    assignment = _assignment()
    task_payload = assignment.to_payload()
    result_payload = encode_source_result(assignment, _result_set(assignment))

    assert task_payload.get("team_id") == _TEAM_A, (
        "source assignment has no explicit canonical team_id; cross-team replay is unbound"
    )
    assert task_payload.get("task_id") == _TASK_A, (
        "source assignment has no explicit canonical task_id; cross-task replay is unbound"
    )
    assert result_payload.get("team_id") == _TEAM_A
    assert result_payload.get("task_id") == _TASK_A


@pytest.mark.parametrize(
    ("identity_key", "replacement"),
    (("team_id", "team-b"), ("task_id", "task-b")),
)
def test_cross_context_result_replay_fails_closed(
    identity_key: str,
    replacement: str,
) -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))
    assert output.get(identity_key), f"result does not bind {identity_key}"

    replay = deepcopy(output)
    replay[identity_key] = replacement
    with pytest.raises(SourceResultBindingError):
        _decode(assignment, replay)


def test_result_content_has_stable_digest_or_evidence_identity_and_detects_substitution() -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))
    digest_keys = ("result_digest", "evidence_digest", "binding_sha256")
    digest_key = next((key for key in digest_keys if output.get(key)), None)
    assert digest_key is not None, (
        "source result carries no stable result/evidence digest identity; a same-context "
        "result body can be substituted without an integrity binding"
    )

    substituted = deepcopy(output)
    result_set = substituted["result_set"]
    assert isinstance(result_set, dict)
    items = result_set["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["snippet"] = "substituted-after-production"

    with pytest.raises(SourceResultBindingError):
        _decode(assignment, substituted)


def test_restart_roundtrip_cannot_replay_result_into_other_team_or_task() -> None:
    assignment = _assignment()
    task_payload = json.loads(json.dumps(assignment.to_payload()))
    output = json.loads(json.dumps(encode_source_result(assignment, _result_set(assignment))))
    restored = SourceInspectionAssignment.from_payload(task_payload)

    restored_fields = {item.name for item in fields(SourceInspectionAssignment)}
    assert {"team_id", "task_id"}.issubset(restored_fields), (
        "restart-restored assignment has no typed team/task identity to reject replay"
    )
    assert getattr(restored, "team_id") == _TEAM_A
    assert getattr(restored, "task_id") == _TASK_A

    other_team = replace(restored, team_id="team-b")
    with pytest.raises(SourceResultBindingError):
        _decode(other_team, output)

    other_task = replace(restored, task_id="task-b")
    with pytest.raises(SourceResultBindingError):
        _decode(other_task, output)
