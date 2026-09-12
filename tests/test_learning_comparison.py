import json

import pytest

from nika_core.learning_comparison import (
    ComparisonEvidenceKind,
    ComparisonEvidenceRef,
    ExperienceMemoryComparison,
    MemoryComparisonResult,
    MemoryRelation,
)

EXPERIENCE_NAMESPACE = "a" * 64
MEMORY_NAMESPACE = "b" * 64
EXPERIENCE_ID = "c" * 64
EXPERIENCE_EVIDENCE = "d" * 64
COMPARATOR = "e" * 64
POLICY = "f" * 64
MEMORY_EVIDENCE = "1" * 64
RELATION_EVIDENCE = "2" * 64


def _experience() -> ComparisonEvidenceRef:
    return ComparisonEvidenceRef(
        kind=ComparisonEvidenceKind.EXPERIENCE,
        source_namespace_sha256=EXPERIENCE_NAMESPACE,
        source_id_sha256=EXPERIENCE_ID,
        evidence_sha256=EXPERIENCE_EVIDENCE,
    )


def _memory(
    source_id_sha256: str,
    *,
    evidence_sha256: str = MEMORY_EVIDENCE,
    namespace_sha256: str = MEMORY_NAMESPACE,
) -> ComparisonEvidenceRef:
    return ComparisonEvidenceRef(
        kind=ComparisonEvidenceKind.MEMORY,
        source_namespace_sha256=namespace_sha256,
        source_id_sha256=source_id_sha256,
        evidence_sha256=evidence_sha256,
    )


def _result(
    source_id_sha256: str,
    *,
    relation: MemoryRelation = MemoryRelation.SUPPORTS,
    memory_sha256: str = MEMORY_EVIDENCE,
    relation_sha256: str = RELATION_EVIDENCE,
) -> MemoryComparisonResult:
    return MemoryComparisonResult(
        memory=_memory(source_id_sha256, evidence_sha256=memory_sha256),
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
        "comparator_sha256": COMPARATOR,
        "comparison_policy_sha256": POLICY,
        "expected_comparator_sha256": COMPARATOR,
        "expected_comparison_policy_sha256": POLICY,
    }
    kwargs.update(overrides)
    return ExperienceMemoryComparison.create(**kwargs)  # type: ignore[arg-type]


def test_comparison_canonicalizes_memory_order_and_has_stable_digest() -> None:
    first = _result("1" * 64, relation=MemoryRelation.SUPPORTS)
    second = _result(
        "2" * 64,
        relation=MemoryRelation.CONTRADICTS,
        memory_sha256="3" * 64,
        relation_sha256="4" * 64,
    )

    comparison_a = _comparison((second, first))
    comparison_b = _comparison((first, second))

    assert tuple(item.memory.source_id_sha256 for item in comparison_a.memory_results) == (
        "1" * 64,
        "2" * 64,
    )
    assert comparison_a.reportable_payload() == comparison_b.reportable_payload()
    assert comparison_a.comparison_sha256 == comparison_b.comparison_sha256


def test_reportable_evidence_contains_only_hashed_source_identity() -> None:
    comparison = _comparison((_result("7" * 64),))

    encoded = json.dumps(comparison.reportable_payload(), sort_keys=True)

    assert "workspace-1" not in encoded
    assert "agent-1" not in encoded
    assert "comparison-1" not in encoded
    assert "source_type" not in encoded
    assert EXPERIENCE_NAMESPACE in encoded
    assert EXPERIENCE_ID in encoded
    assert MEMORY_NAMESPACE in encoded
    assert "7" * 64 in encoded
    assert COMPARATOR in encoded
    assert POLICY in encoded


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
    overrides = {field: "0" * 64, trusted_field: "9" * 64}

    with pytest.raises(ValueError, match="trusted authority"):
        _comparison((_result("1" * 64),), **overrides)


def test_duplicate_logical_memory_reference_is_rejected_even_with_new_digest() -> None:
    first = _result("1" * 64, memory_sha256="3" * 64)
    replacement = _result("1" * 64, memory_sha256="4" * 64)

    with pytest.raises(ValueError, match="duplicate logical memory"):
        _comparison((first, replacement))


def test_same_memory_id_in_different_namespaces_remains_distinct() -> None:
    first = _result("1" * 64)
    second = MemoryComparisonResult(
        memory=_memory("1" * 64, namespace_sha256="9" * 64),
        relation=MemoryRelation.EXTENDS,
        relation_evidence_sha256="8" * 64,
    )

    comparison = _comparison((second, first))

    assert len(comparison.memory_results) == 2


def test_empty_and_oversized_memory_sets_are_rejected() -> None:
    with pytest.raises(ValueError, match="count is outside"):
        _comparison(())

    results = tuple(_result(f"{index:064x}") for index in range(65))
    with pytest.raises(ValueError, match="count is outside"):
        _comparison(results)


def test_mutable_memory_collection_is_rejected() -> None:
    with pytest.raises(TypeError, match="immutable tuple"):
        ExperienceMemoryComparison.create(
            comparison_id="comparison-1",
            workspace_id="workspace-1",
            agent_id="agent-1",
            experience=_experience(),
            memory_results=[_result("1" * 64)],  # type: ignore[arg-type]
            comparator_sha256=COMPARATOR,
            comparison_policy_sha256=POLICY,
            expected_comparator_sha256=COMPARATOR,
            expected_comparison_policy_sha256=POLICY,
        )


def test_evidence_domains_cannot_be_swapped() -> None:
    with pytest.raises(ValueError, match="memory evidence kind"):
        MemoryComparisonResult(
            memory=_experience(),
            relation=MemoryRelation.SUPPORTS,
            relation_evidence_sha256=RELATION_EVIDENCE,
        )

    with pytest.raises(ValueError, match="experience evidence kind"):
        _comparison(
            (_result("1" * 64),),
            experience=_memory("9" * 64),
        )


def test_raw_relation_string_is_rejected() -> None:
    with pytest.raises(TypeError, match="MemoryRelation"):
        MemoryComparisonResult(
            memory=_memory("1" * 64),
            relation="supports",  # type: ignore[arg-type]
            relation_evidence_sha256=RELATION_EVIDENCE,
        )


def test_authority_bearing_strings_reject_string_subclasses() -> None:
    class Spoof(str):
        pass

    with pytest.raises(TypeError, match="source_id_sha256 must be an exact string"):
        ComparisonEvidenceRef(
            kind=ComparisonEvidenceKind.MEMORY,
            source_namespace_sha256=MEMORY_NAMESPACE,
            source_id_sha256=Spoof("1" * 64),
            evidence_sha256=MEMORY_EVIDENCE,
        )

    with pytest.raises(TypeError, match="expected_comparator_sha256"):
        _comparison(
            (_result("1" * 64),),
            expected_comparator_sha256=Spoof(COMPARATOR),
        )


def test_noncanonical_sha256_is_rejected() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ComparisonEvidenceRef(
            kind=ComparisonEvidenceKind.MEMORY,
            source_namespace_sha256=MEMORY_NAMESPACE,
            source_id_sha256="A" * 64,
            evidence_sha256=MEMORY_EVIDENCE,
        )


def test_relation_and_relation_evidence_change_comparison_identity() -> None:
    supporting = _comparison(
        (_result("1" * 64, relation=MemoryRelation.SUPPORTS),)
    )
    contradicting = _comparison(
        (_result("1" * 64, relation=MemoryRelation.CONTRADICTS),)
    )
    new_evidence = _comparison(
        (_result("1" * 64, relation_sha256="3" * 64),)
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
            memory_results=(_result("1" * 64),),
            comparator_sha256=COMPARATOR,
            comparison_policy_sha256=POLICY,
        )
