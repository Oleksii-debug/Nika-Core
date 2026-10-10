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
        query="test",
        created_at="2026-10-04T00:00:00+00:00",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id="document-1",
                title="Document",
                snippet="Text",
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


def _resign(output: dict[str, object]) -> None:
    unsigned = {key: value for key, value in output.items() if key != "result_digest"}
    output["result_digest"] = hashlib.sha256(
        json.dumps(
            unsigned, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("rank", [10**400, -(10**400), float("inf"), -float("inf"), float("nan")])
def test_encode_rejects_nonfinite_rank_without_leaking_overflow(rank: object) -> None:
    assignment, result = _case()
    forged = replace(result, items=(replace(result.items[0], rank=rank),))
    with pytest.raises(SourceResultBindingError, match="result rank must be finite"):
        encode_source_result(assignment, forged)


def test_decode_rejects_large_integer_rank_with_recomputed_digest() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["rank"] = 10**400
    _resign(output)
    with pytest.raises(SourceResultBindingError, match="result rank must be finite"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_decode_rejects_oversized_list_before_digest_serialization() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    # A non-JSON element proves validation ran before serializing attacker-owned contents.
    result_data["items"] = [items[0], {"rank": {1, 2}}]
    with pytest.raises(SourceResultBindingError, match="result exceeds assignment max_items"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_ordinary_finite_rank_retains_json_restart_roundtrip() -> None:
    assignment, result = _case()
    for rank in (0.5, -0.25, 2):
        candidate = replace(result, items=(replace(result.items[0], rank=rank),))
        output = json.loads(json.dumps(encode_source_result(assignment, candidate)))
        restored = decode_source_result(assignment, member_id=assignment.member_id, output=output)
        assert restored.items[0].rank == rank


@pytest.mark.parametrize("invalid", ["short", "G" * 64, "A" * 64, "0" * 65])
def test_invalid_digest_fails_before_worker_payload_serialization(invalid: str) -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["snippet"] = {1, 2}
    output["result_digest"] = invalid
    with pytest.raises(SourceResultBindingError, match="invalid result_digest"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_malformed_item_container_fails_before_digest_serialization() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    result_data["items"] = {"forged": {1, 2}}
    with pytest.raises(SourceResultBindingError, match="result_set items must be a list"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_unexpected_result_fields_fail_before_digest_serialization() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    result_data["unrecognized"] = {1, 2}
    with pytest.raises(SourceResultBindingError, match="result_set fields do not match schema"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_bool_rank_still_fails_as_non_numeric_after_resigning() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["rank"] = True
    _resign(output)
    with pytest.raises(SourceResultBindingError, match="result rank must be numeric"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)



def test_encode_rejects_oversized_text_before_digest_hashing() -> None:
    assignment, result = _case()
    forged = replace(
        result,
        items=(replace(result.items[0], snippet="x" * 1_048_577),),
    )
    with pytest.raises(SourceResultBindingError, match="bounded JSON"):
        encode_source_result(assignment, forged)


def test_decode_rejects_oversized_text_before_digest_hashing() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["snippet"] = "x" * 1_048_577
    output["result_digest"] = "0" * 64
    with pytest.raises(SourceResultBindingError, match="bounded JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_encode_rejects_unbounded_evidence_fanout_before_serialization() -> None:
    assignment, result = _case()
    evidence = result.items[0].evidence * 10_001
    forged = replace(
        result,
        items=(replace(result.items[0], evidence=evidence),),
    )
    with pytest.raises(SourceResultBindingError, match="bounded JSON"):
        encode_source_result(assignment, forged)


def test_decode_rejects_evidence_node_fanout_before_digest_hashing() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    evidence = items[0]["evidence"]
    assert isinstance(evidence, list)
    items[0]["evidence"] = evidence * 2_000
    output["result_digest"] = "0" * 64
    with pytest.raises(SourceResultBindingError, match="bounded JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_decode_rejects_oversized_integer_before_digest_hashing() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    items[0]["ordinal"] = 1 << 4_097
    output["result_digest"] = "0" * 64
    with pytest.raises(SourceResultBindingError, match="bounded JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_decode_rejects_cyclic_worker_value_without_recursion_leak() -> None:
    assignment, result = _case()
    output = encode_source_result(assignment, result)
    result_data = output["result_set"]
    assert isinstance(result_data, dict)
    items = result_data["items"]
    assert isinstance(items, list)
    cycle: list[object] = []
    cycle.append(cycle)
    items[0]["snippet"] = cycle
    output["result_digest"] = "0" * 64
    with pytest.raises(SourceResultBindingError, match="canonical JSON"):
        decode_source_result(assignment, member_id=assignment.member_id, output=output)


def test_encode_rejects_invalid_utf8_text_before_digest_hashing() -> None:
    assignment, result = _case()
    forged = replace(
        result,
        items=(replace(result.items[0], snippet="bad\ud800text"),),
    )
    with pytest.raises(SourceResultBindingError, match="valid UTF-8"):
        encode_source_result(assignment, forged)
