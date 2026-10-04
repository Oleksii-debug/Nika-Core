from __future__ import annotations

import hashlib
import json
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
        query="Ніка: canonical JSON",
        created_at="2026-10-04T00:00:00+00:00",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id="document-1",
                title="Українська назва",
                snippet='Text "quoted"\\nwith escaped newline',
                rank=0.5,
                why_matched="test fixture",
                evidence=(
                    ResearchEvidence(
                        source_id=source.source_id,
                        source_kind=source.kind,
                        locator=source.locator,
                        observed_at="2026-10-04T00:00:00+00:00",
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
    )
    return assignment, result


def test_digest_preserves_original_canonical_json_bytes() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    unsigned = {key: value for key, value in output.items() if key != "result_digest"}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert output["result_digest"] == hashlib.sha256(encoded).hexdigest()
    assert decode_source_result(assignment, member_id=assignment.member_id, output=output) == result


@pytest.mark.parametrize(
    ("character", "repeat"),
    [("A", 8 * 1024 * 1024), ("Ї", 4 * 1024 * 1024 + 1)],
)
def test_encode_rejects_oversized_evidence_before_full_buffering(
    character: str, repeat: int
) -> None:
    assignment, result = _case()
    oversized = replace(result.items[0], snippet=character * repeat)
    with pytest.raises(SourceResultBindingError, match="exceeds maximum JSON size"):
        encode_source_result(assignment, replace(result, items=(oversized,)))


def test_decode_rejects_oversized_payload_before_comparing_digest() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["snippet"] = "A" * (8 * 1024 * 1024)
    # This is intentionally still the old, well-formed digest. The size
    # boundary precedes the signature comparison and never trusts it.
    with pytest.raises(SourceResultBindingError, match="exceeds maximum JSON size"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_invalid_unicode_is_a_bound_binding_error_on_both_paths() -> None:
    assignment, result = _case()
    bad_item = replace(result.items[0], snippet="\ud800")
    with pytest.raises(SourceResultBindingError, match="canonical JSON"):
        encode_source_result(assignment, replace(result, items=(bad_item,)))

    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["snippet"] = "\ud800"
    with pytest.raises(SourceResultBindingError, match="canonical JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_large_valid_multibyte_result_survives_json_restart() -> None:
    assignment, result = _case()
    snippet = "Ї" * 65536
    candidate = replace(result, items=(replace(result.items[0], snippet=snippet),))
    output = json.loads(json.dumps(encode_source_result(assignment, candidate)))
    restored = decode_source_result(assignment, member_id=assignment.member_id, output=output)
    assert restored.items[0].snippet == snippet
