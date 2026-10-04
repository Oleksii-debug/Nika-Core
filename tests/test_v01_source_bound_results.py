from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

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


def _assignment(
    *,
    team_id: str = "team-a",
    task_id: str = "task-a",
    member_id: str = "worker-a",
    source_id: str = "source-a",
    workspace_id: str = "workspace-a",
    locator: str = "fixture://source-a",
    tool_call_id: str = "tool-call-a",
    effect_id: str = "effect-a",
    assignment_id: str = "assignment-a",
    max_items: int = 2,
) -> SourceInspectionAssignment:
    return SourceInspectionAssignment(
        team_id=team_id,
        task_id=task_id,
        assignment_id=assignment_id,
        member_id=member_id,
        source=SourceSpec(
            source_id=source_id,
            workspace_id=workspace_id,
            kind=SourceKind.LOCAL_FILE,
            locator=locator,
        ),
        tool_call_id=tool_call_id,
        effect_id=effect_id,
        max_items=max_items,
    )


def _result_set(
    assignment: SourceInspectionAssignment,
    *,
    snippet: str = "alpha",
    item_count: int = 1,
) -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id="result-a",
        workspace_id=assignment.source.workspace_id,
        query="declared query",
        created_at="2026-09-03T00:00:00+00:00",
        items=tuple(
            ResearchResultItem(
                ordinal=index,
                document_id=f"doc-{index}",
                title=f"fixture-{index}",
                snippet=snippet,
                rank=1.0 - (index / 10),
                why_matched="deterministic fixture",
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
            for index in range(item_count)
        ),
    )


def _decode(
    assignment: SourceInspectionAssignment,
    output: dict[str, object],
) -> ResearchResultSet:
    return decode_source_result(
        assignment,
        member_id=assignment.member_id,
        output=output,
    )


def test_assignment_and_result_bind_independent_team_task_and_effect_identities() -> None:
    assignment = _assignment()
    task_payload = assignment.to_payload()
    result_payload = encode_source_result(assignment, _result_set(assignment))

    assert task_payload["team_id"] == "team-a"
    assert task_payload["task_id"] == "task-a"
    assert task_payload["effect_id"] == "effect-a"
    assert result_payload["team_id"] == "team-a"
    assert result_payload["task_id"] == "task-a"
    assert result_payload["result_digest"]
    assert _decode(assignment, result_payload).items[0].snippet == "alpha"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("team_id", "team-b"),
        ("task_id", "task-b"),
        ("assignment_id", "assignment-b"),
        ("member_id", "worker-b"),
        ("source_id", "source-b"),
        ("source_kind", "http"),
        ("workspace_id", "workspace-b"),
        ("locator", "fixture://source-b"),
        ("tool_call_id", "tool-call-b"),
        ("effect_id", "effect-b"),
    ),
)
def test_outer_identity_substitution_fails_closed(field: str, replacement: str) -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))
    output[field] = replacement

    with pytest.raises(SourceResultBindingError, match=field):
        _decode(assignment, output)


def test_content_substitution_is_detected_by_stable_result_digest() -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))
    substituted = deepcopy(output)
    result_set = substituted["result_set"]
    assert isinstance(result_set, dict)
    items = result_set["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["snippet"] = "substituted after worker completion"

    with pytest.raises(SourceResultBindingError, match="result_digest"):
        _decode(assignment, substituted)


def test_recomputed_digest_cannot_bypass_nested_source_provenance_validation() -> None:
    assignment = _assignment()
    output = encode_source_result(assignment, _result_set(assignment))
    result_set = deepcopy(output["result_set"])
    assert isinstance(result_set, dict)
    items = result_set["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    evidence = first["evidence"]
    assert isinstance(evidence, list)
    first_evidence = evidence[0]
    assert isinstance(first_evidence, dict)
    first_evidence["source_id"] = "source-b"

    forged = encode_source_result(assignment, _result_set(assignment))
    forged["result_set"] = result_set
    # A malicious worker can recompute its own digest, so provenance validation remains mandatory.
    unsigned = {key: value for key, value in forged.items() if key != "result_digest"}
    forged["result_digest"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(SourceResultBindingError, match="evidence source_id"):
        _decode(assignment, forged)


def test_json_restart_roundtrip_rejects_cross_team_and_cross_task_replay() -> None:
    assignment = _assignment()
    restored = SourceInspectionAssignment.from_payload(
        json.loads(json.dumps(assignment.to_payload()))
    )
    output = json.loads(json.dumps(encode_source_result(assignment, _result_set(assignment))))

    assert restored == assignment
    with pytest.raises(SourceResultBindingError, match="team_id"):
        _decode(replace(restored, team_id="team-other"), output)
    with pytest.raises(SourceResultBindingError, match="task_id"):
        _decode(replace(restored, task_id="task-other"), output)


def test_result_limit_and_duplicate_document_identity_fail_closed() -> None:
    assignment = _assignment(max_items=1)
    with pytest.raises(SourceResultBindingError, match="max_items"):
        encode_source_result(assignment, _result_set(assignment, item_count=2))

    duplicate = _result_set(_assignment(), item_count=2)
    duplicate = replace(
        duplicate,
        items=(duplicate.items[0], replace(duplicate.items[1], document_id="doc-0")),
    )
    with pytest.raises(SourceResultBindingError, match="document_id"):
        encode_source_result(_assignment(), duplicate)
