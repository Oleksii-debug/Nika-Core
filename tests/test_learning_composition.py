from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
)
from nika_core.learning_comparison import (
    ComparisonEvidenceKind,
    ComparisonEvidenceRef,
    ExperienceMemoryComparison,
    MemoryComparisonResult,
    MemoryRelation,
)
from nika_core.learning_composition import (
    build_learning_semantic_update_executor,
    build_learning_semantic_update_router,
    cognition_candidate_from_comparisons,
    cognition_evidence_from_comparison,
)
from nika_core.learning_semantic_execution import LearningSemanticUpdateExecutor
from nika_core.learning_semantic_update import LearningSemanticUpdateRouter
from nika_core.memory.service import MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger

_COMPARATOR = "a" * 64
_POLICY = "b" * 64


def _evidence(
    *,
    kind: ComparisonEvidenceKind,
    namespace_char: str,
    source_char: str,
    evidence_char: str,
) -> ComparisonEvidenceRef:
    return ComparisonEvidenceRef(
        kind=kind,
        source_namespace_sha256=namespace_char * 64,
        source_id_sha256=source_char * 64,
        evidence_sha256=evidence_char * 64,
    )


def _comparison(
    comparison_id: str,
    *,
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
    relation_evidence_char: str = "6",
) -> ExperienceMemoryComparison:
    experience = _evidence(
        kind=ComparisonEvidenceKind.EXPERIENCE,
        namespace_char="1",
        source_char="2",
        evidence_char="3",
    )
    memory = _evidence(
        kind=ComparisonEvidenceKind.MEMORY,
        namespace_char="4",
        source_char="5",
        evidence_char="7",
    )
    result = MemoryComparisonResult(
        memory=memory,
        relation=MemoryRelation.SUPPORTS,
        relation_evidence_sha256=relation_evidence_char * 64,
    )
    return ExperienceMemoryComparison.create(
        comparison_id=comparison_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        experience=experience,
        memory_results=(result,),
        comparator_sha256=_COMPARATOR,
        comparison_policy_sha256=_POLICY,
        expected_comparator_sha256=_COMPARATOR,
        expected_comparison_policy_sha256=_POLICY,
    )


def _cognition_evidence(
    comparison: ExperienceMemoryComparison,
) -> CognitionEvidenceRef:
    return cognition_evidence_from_comparison(
        comparison,
        expected_comparator_sha256=_COMPARATOR,
        expected_comparison_policy_sha256=_POLICY,
    )


def _cognition_candidate(
    *,
    candidate_id: str,
    kind: CognitionCandidateKind,
    statement: str,
    comparisons: tuple[ExperienceMemoryComparison, ...],
) -> CognitionCandidate:
    return cognition_candidate_from_comparisons(
        candidate_id=candidate_id,
        kind=kind,
        statement=statement,
        comparisons=comparisons,
        expected_comparator_sha256=_COMPARATOR,
        expected_comparison_policy_sha256=_POLICY,
    )


def _forged_comparison(
    *,
    comparator_sha256: str = "c" * 64,
    comparison_policy_sha256: str = "d" * 64,
) -> ExperienceMemoryComparison:
    canonical = _comparison("comparison-forged")
    forged = object.__new__(ExperienceMemoryComparison)
    for field in (
        "comparison_id",
        "workspace_id",
        "agent_id",
        "experience",
        "memory_results",
    ):
        object.__setattr__(forged, field, getattr(canonical, field))
    object.__setattr__(forged, "comparator_sha256", comparator_sha256)
    object.__setattr__(
        forged,
        "comparison_policy_sha256",
        comparison_policy_sha256,
    )
    return forged


def test_comparison_evidence_ref_binds_exact_comparison_identity() -> None:
    comparison = _comparison("comparison-1")

    evidence = _cognition_evidence(comparison)

    assert evidence.source_type == "experience_memory_comparison"
    assert evidence.source_id == comparison.comparison_id
    assert evidence.evidence_sha256 == comparison.comparison_sha256


def test_hypothesis_inherits_comparison_workspace_and_agent() -> None:
    comparison = _comparison("comparison-1")

    candidate = _cognition_candidate(
        candidate_id="candidate-1",
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="The prior outcome supports retrying the bounded step.",
        comparisons=(comparison,),
    )

    assert candidate.workspace_id == comparison.workspace_id
    assert candidate.agent_id == comparison.agent_id
    assert len(candidate.evidence) == 1
    assert candidate.evidence[0].evidence_sha256 == comparison.comparison_sha256


def test_abstraction_binds_multiple_same_scope_comparisons() -> None:
    first = _comparison("comparison-1", relation_evidence_char="6")
    second = _comparison("comparison-2", relation_evidence_char="8")

    candidate = _cognition_candidate(
        candidate_id="candidate-1",
        kind=CognitionCandidateKind.ABSTRACTION,
        statement="Repeated verified outcomes suggest a reusable bounded procedure.",
        comparisons=(second, first),
    )

    assert candidate.kind is CognitionCandidateKind.ABSTRACTION
    assert candidate.workspace_id == "workspace-1"
    assert candidate.agent_id == "agent-1"
    assert {item.evidence_sha256 for item in candidate.evidence} == {
        first.comparison_sha256,
        second.comparison_sha256,
    }


def test_cross_workspace_or_agent_composition_fails_closed() -> None:
    canonical = _comparison("comparison-1")
    other_workspace = _comparison("comparison-2", workspace_id="workspace-2")
    other_agent = _comparison("comparison-3", agent_id="agent-2")

    with pytest.raises(ValueError, match="same workspace"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.ABSTRACTION,
            statement="Cross-workspace evidence must not compose.",
            comparisons=(canonical, other_workspace),
        )
    with pytest.raises(ValueError, match="same agent"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.ABSTRACTION,
            statement="Cross-agent evidence must not compose.",
            comparisons=(canonical, other_agent),
        )


def test_duplicate_comparison_identity_rejected_even_if_digest_differs() -> None:
    first = _comparison("comparison-1", relation_evidence_char="6")
    second = _comparison("comparison-1", relation_evidence_char="8")
    assert first.comparison_sha256 != second.comparison_sha256

    with pytest.raises(ValueError, match="duplicate comparison identities"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.ABSTRACTION,
            statement="Duplicate comparison identity must fail closed.",
            comparisons=(first, second),
        )


def test_collection_shape_and_bounds_fail_closed() -> None:
    comparison = _comparison("comparison-1")

    with pytest.raises(TypeError, match="immutable tuple"):
        cognition_candidate_from_comparisons(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.HYPOTHESIS,
            statement="Lists are not canonical comparison collections.",
            comparisons=[comparison],  # type: ignore[arg-type]
            expected_comparator_sha256=_COMPARATOR,
            expected_comparison_policy_sha256=_POLICY,
        )
    with pytest.raises(ValueError, match="count"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.HYPOTHESIS,
            statement="An empty comparison set cannot support cognition.",
            comparisons=(),
        )
    with pytest.raises(ValueError, match="count"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.HYPOTHESIS,
            statement="Oversized comparison ingress must fail before composition.",
            comparisons=(comparison,) * 65,
        )


def test_noncanonical_comparison_type_cannot_cross_composition_boundary() -> None:
    class SpoofComparison(ExperienceMemoryComparison):
        pass

    spoof = object.__new__(SpoofComparison)

    with pytest.raises(TypeError, match="ExperienceMemoryComparison"):
        _cognition_evidence(spoof)
    with pytest.raises(TypeError, match="ExperienceMemoryComparison"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.HYPOTHESIS,
            statement="A subclass must not inherit comparison authority.",
            comparisons=(spoof,),  # type: ignore[arg-type]
        )


def test_exact_type_forgery_cannot_choose_comparator_or_policy_authority() -> None:
    forged = _forged_comparison()

    with pytest.raises(ValueError, match="trusted authority"):
        _cognition_evidence(forged)
    with pytest.raises(ValueError, match="trusted authority"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.HYPOTHESIS,
            statement="Forged comparison authority must not reach cognition.",
            comparisons=(forged,),
        )


def test_exact_type_forgery_revalidates_nested_evidence() -> None:
    canonical = _comparison("comparison-forged-nested")
    forged_experience = object.__new__(ComparisonEvidenceRef)
    object.__setattr__(
        forged_experience,
        "kind",
        ComparisonEvidenceKind.EXPERIENCE,
    )
    object.__setattr__(
        forged_experience,
        "source_namespace_sha256",
        "not-a-sha",
    )
    object.__setattr__(forged_experience, "source_id_sha256", "2" * 64)
    object.__setattr__(forged_experience, "evidence_sha256", "3" * 64)

    forged = object.__new__(ExperienceMemoryComparison)
    for field in (
        "comparison_id",
        "workspace_id",
        "agent_id",
        "memory_results",
        "comparator_sha256",
        "comparison_policy_sha256",
    ):
        object.__setattr__(forged, field, getattr(canonical, field))
    object.__setattr__(forged, "experience", forged_experience)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _cognition_evidence(forged)


def test_abstraction_still_requires_multiple_comparison_evidence_refs() -> None:
    comparison = _comparison("comparison-1")

    with pytest.raises(ValueError, match="at least two evidence references"):
        _cognition_candidate(
            candidate_id="candidate-1",
            kind=CognitionCandidateKind.ABSTRACTION,
            statement="One comparison is insufficient for an abstraction.",
            comparisons=(comparison,),
        )


def test_reportable_candidate_excludes_raw_scope_comparison_and_statement() -> None:
    comparison = _comparison("comparison-private-1")
    statement = "private transient hypothesis CANARY-LOOP-B-42"

    candidate = _cognition_candidate(
        candidate_id="candidate-private-1",
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement=statement,
        comparisons=(comparison,),
    )
    serialized = json.dumps(candidate.reportable_payload(), sort_keys=True)

    assert candidate.candidate_id not in serialized
    assert candidate.workspace_id not in serialized
    assert candidate.agent_id not in serialized
    assert comparison.comparison_id not in serialized
    assert statement not in serialized
    assert "CANARY-LOOP-B-42" not in serialized
    assert comparison.comparison_sha256 in serialized

def test_semantic_router_factory_reuses_canonical_memory_without_new_schema(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "semantic-composition.db")
    store.initialize()
    with store.connection() as conn:
        before = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )

    router = build_learning_semantic_update_router(MemoryService(store))

    with store.connection() as conn:
        after = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )
    assert type(router) is LearningSemanticUpdateRouter
    assert after == before


def test_semantic_router_factory_rejects_noncanonical_memory_service(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "semantic-composition-type.db")
    store.initialize()

    class DerivedMemoryService(MemoryService):
        pass

    with pytest.raises(TypeError, match="canonical MemoryService"):
        build_learning_semantic_update_router(DerivedMemoryService(store))



def test_semantic_executor_factory_reuses_canonical_authorities_without_new_schema(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "semantic-executor-composition.db")
    store.initialize()
    with store.connection() as conn:
        before = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )

    executor = build_learning_semantic_update_executor(
        MemoryService(store),
        IdempotencyLedger(store),
    )

    with store.connection() as conn:
        after = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )
    assert type(executor) is LearningSemanticUpdateExecutor
    assert after == before


def test_semantic_executor_factory_rejects_noncanonical_idempotency(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "semantic-executor-composition-type.db")
    store.initialize()

    class DerivedIdempotencyLedger(IdempotencyLedger):
        pass

    with pytest.raises(TypeError, match="canonical IdempotencyLedger"):
        build_learning_semantic_update_executor(
            MemoryService(store),
            DerivedIdempotencyLedger(store),
        )

def test_semantic_executor_factory_rejects_split_sqlite_authorities(
    tmp_path: Path,
) -> None:
    memory_store = SQLiteStore(tmp_path / "semantic-memory.db")
    ledger_store = SQLiteStore(tmp_path / "semantic-ledger.db")
    memory_store.initialize()
    ledger_store.initialize()

    with pytest.raises(ValueError, match="share one SQLiteStore"):
        build_learning_semantic_update_executor(
            MemoryService(memory_store),
            IdempotencyLedger(ledger_store),
        )
