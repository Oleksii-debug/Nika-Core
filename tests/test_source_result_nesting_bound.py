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


def _valid_output() -> tuple[SourceInspectionAssignment, dict[str, object]]:
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
    result_set = ResearchResultSet(
        result_set_id="result-1",
        workspace_id=source.workspace_id,
        query="Ніка: bounded research",
        created_at="2026-10-04T00:00:00+00:00",
        items=(),
    )
    return assignment, encode_source_result(assignment, result_set)


@pytest.mark.parametrize("shape", ["deep-list", "deep-mapping", "cycle"])
def test_untrusted_nested_result_cannot_escape_as_recursion_error(shape: str) -> None:
    assignment, output = _valid_output()
    # The ordinary, canonical result is still accepted before the adversarial change.
    assert decode_source_result(
        assignment, member_id=assignment.member_id, output=output
    ).query == "Ніка: bounded research"

    if shape == "cycle":
        value: object = []
        assert isinstance(value, list)
        value.append(value)
    else:
        value = "leaf"
        # This value is small in bytes but exceeds JSONEncoder's recursion budget.
        for _ in range(sys.getrecursionlimit() + 64):
            value = [value] if shape == "deep-list" else {"nested": value}

    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    result_data["query"] = value
    # Retain the syntactically valid old digest: the digest boundary must reject
    # malformed evidence before claiming a verified worker result.
    with pytest.raises(SourceResultBindingError, match="result evidence must be canonical JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)
