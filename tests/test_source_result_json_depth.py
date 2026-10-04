from __future__ import annotations

import sys

import pytest

from nika_core.multi_agent.research_results import (
    SourceInspectionAssignment,
    SourceResultBindingError,
    decode_source_result,
    encode_source_result,
)
from nika_core.research.models import ResearchResultSet, SourceKind, SourceSpec


def _case() -> tuple[SourceInspectionAssignment, ResearchResultSet]:
    source = SourceSpec("source-1", "workspace-1", SourceKind.LOCAL_FILE, "fixture://source-1")
    assignment = SourceInspectionAssignment(
        team_id="team-1",
        task_id="task-1",
        assignment_id="assignment-1",
        member_id="member-1",
        source=source,
        tool_call_id="call-1",
        effect_id="effect-1",
        max_items=1,
    )
    result = ResearchResultSet(
        result_set_id="result-1",
        workspace_id=source.workspace_id,
        query="ordinary query",
        created_at="2026-10-04T00:00:00+00:00",
        items=(),
    )
    return assignment, result


def test_deep_worker_field_is_a_sanitized_binding_error() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    raw_result = output["result_set"]
    assert isinstance(raw_result, dict)

    # The outer schema, item count and claimed digest remain well-formed.
    # The hostile value fits well below the byte budget but exceeds JSON
    # encoder recursion depth before digest comparison or typed decoding.
    nested: object = "forged query"
    for _ in range(sys.getrecursionlimit() + 64):
        nested = [nested]
    raw_result["query"] = nested

    with pytest.raises(SourceResultBindingError, match="canonical JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)

    # The rejection must not poison the incumbent successful result path.
    valid = encode_source_result(assignment, result)
    assert decode_source_result(
        assignment, member_id=assignment.member_id, output=valid
    ) == result
