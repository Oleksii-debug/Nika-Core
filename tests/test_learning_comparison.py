import json

import pytest

from nika_core.learning_comparison import (
    ComparisonEvidenceRef,
    ExperienceMemoryComparison,
    MemoryComparisonResult,
    MemoryRelation,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _experience() -> ComparisonEvidenceRef:
    return ComparisonEvidenceRef(
        source_type="experience_ledger",
        source_id="experience-1",
        evidence_sha256=SHA_A,
    )


def _memory(
    source_id: str,
    *,
    evidence_sha256: str = SHA_B,
) -> ComparisonEvidenceRef:
    return ComparisonEvidenceRef(
        source_type="memory",
        source_id=source_id,
        evidence_sha256=evidence_sha256,
    )


def _result(
    source_id: str,
    *,
    relation: MemoryRelation = MemoryRelation.SUPPORTS,
    memory_sha256: str = SHA_B,
    relation_sha256: str = SHA_C,
) -> MemoryComparisonResult:
    return MemoryComparisonResult(
        memory=_memory(source_id, evidence_sha256=memory_sha256),
        relation=relation,
        relation_evidence_sha256=relation_sha256,
    )


def _comparison(
    memory_results: tuple[MemoryComparisonResult, ...],
    **overrides: object,
) -> ExperienceMemoryComparison:
    kwargs: dict[str, object] = {
        "comparison_id": "comparison-1",
        "workspace_id": "workspace-1",
        "agent_id": "agent-1",
        "experience": _experience(),
        "memory_results": memory_results,
        "comparator_sha256": SHA_D,
        "comparison_policy_sha256": SHA_E,
        "expected_comparator_sha256": SHA_D,
        "expected_comparison_policy_sha256": SHA_E,
    }
    kwargs.update(overrides)
    return ExperienceMemoryComparison.create(**kwargs)  # type: ignore[arg-type]


def test_comparison_canonicalizes_memory_order_and_has_stable_digest() -> None:
    first = _result("memory-a", relation=MemoryRelation.SUPPORTS)
    second = _result(
        "memory-b",
        relation=MemoryRelation.CONTRADICTS,
        memory_sha256=SHA_C,
        relation_sha256=SHA_B,
    )

    comparison_a = _comparison((second, first))
    comparison_b = _comparison((first, second))

    assert tuple(item.memory.source_id for item in comparison_a.memory_results) == (
        "memory-a",
        "memory-b",
    )
    assert comparison_a.reportable_payload() == comparison_b.reportable_payload()
    assert comparison_a.comparison_sha256 == comparison_b.comparison_sha256


def test_reportable_evidence_minimizes_scope_and_source_ids() -> None:
    comparison = _comparison((_result("private-memory-id"),))

    encoded = json.dumps(comparison.reportable_payload(), sort_keys=True)

    assert "workspace-1" not in encoded
    assert "agent-1" not in encoded
    assert "comparison-1" not in encoded
    assert "experience-1" not in encoded
    assert "private-memory-id" not in encoded
    assert SHA_A in encoded
    assert SHA_D in encoded
    assert SHA_E in encoded


@pytest.mark.parametrize(
    ("field", "trusted_field"),
    [
        ("comparator_sha256", "expected_comparator_sha256"),
        ("comparison_policy_sha256", "expected_comparison_policy_sha256"),
    ],
)
def test_untrusted_comparator_or_policy_identity_fails_closed(
    field: str,
    trusted_field: str,
) -> None:
    overrides = {field: SHA_A, trusted_field: SHA_B}

    with pytest.raises(ValueError, match="trusted authority"):
        _comparison((_result("memory-a"),), **overrides)


def test_duplicate_logical_memory_reference_is_rejected_even_with_new_digest() -> None:
    first = _result("memory-a", memory_sha256=SHA_A)
    replacement = _result("memory-a", memory_sha256=SHA_B)

    with pytest.raises(ValueError, match="duplicate logical memory"):
        _comparison((first, replacement))


def test_empty_and_oversized_memory_sets_are_rejected() -> None:
    with pytest.raises(ValueError, match="count is outside"):
        _comparison(())

    results = tuple(_result(f"memory-{index}") for index in range(65))
    with pytest.raises(ValueError, match="count is outside"):
        _comparison(results)


def test_mutable_memory_collection_is_rejected() -> None:
    with pytest.raises(TypeError, match="immutable tuple"):
        _comparison(()) .create  # pragma: no cover

    with pytest.raises(TypeError, match="immutable tuple"):
        ExperienceMemoryComparison.create(
            comparison_id="comparison-1",
            workspace_id="workspace-1",
            agent_id="agent-1",
            experience=_experience(),
            memory_results=[_result("memory-a")],  # type: ignore[arg-type]
            comparator_sha256=SHA_D,
            comparison_policy_sha256=SHA_E,
            expected_comparator_sha256=SHA_D,
            expected_comparison_policy_sha256=SHA_E,
        )


def test_raw_relation_string_is_rejected() -> None:
    with pytest.raises(TypeError, match="MemoryRelation"):
        MemoryComparisonResult(
            memory=_memory("memory-a"),
            relation="supports",  # type: ignore[arg-type]
            relation_evidence_sha256=SHA_C,
        )


def test_authority_bearing_strings_reject_string_subclasses() -> None:
    class Spoof(str):
        pass

    with pytest.raises(TypeError, match="source_id must be an exact string"):
        ComparisonEvidenceRef(
            source_type="memory",
            source_id=Spoof("memory-a"),
            evidence_sha256=SHA_A,
        )

    with pytest.raises(TypeError, match="expected_comparator_sha256"):
        _comparison(
            (_result("memory-a"),),
            expected_comparator_sha256=Spoof(SHA_D),
        )


def test_noncanonical_sha256_is_rejected() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ComparisonEvidenceRef(
            source_type="memory",
            source_id="memory-a",
            evidence_sha256="A" * 64,
        )


def test_relation_and_relation_evidence_change_comparison_identity() -> None:
    supporting = _comparison((_result("memory-a", relation=MemoryRelation.SUPPORTS),))
    contradicting = _comparison(
        (_result("memory-a", relation=MemoryRelation.CONTRADICTS),)
    )
    new_evidence = _comparison(
        (_result("memory-a", relation_sha256=SHA_B),)
    )

    assert supporting.comparison_sha256 != contradicting.comparison_sha256
    assert supporting.comparison_sha256 != new_evidence.comparison_sha256


def test_direct_comparison_construction_cannot_bypass_trusted_factory() -> None:
    with pytest.raises(TypeError):
        ExperienceMemoryComparison(
            comparison_id="comparison-1",
            workspace_id="workspace-1",
            agent_id="agent-1",
            experience=_experience(),
            memory_results=(_result("memory-a"),),
            comparator_sha256=SHA_D,
            comparison_policy_sha256=SHA_E,
        )
