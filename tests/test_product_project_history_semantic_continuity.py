from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGenerationService,
)
from nika_core.product_project_history_semantic_continuity import (
    ProductProjectHistorySemanticContinuityService,
)


def _spec(revision: int = 0, requirement_count: int = 6) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a generic long-lived product",
        desired_outcome="PF12 semantic continuity across cold and hot history",
        requirements=tuple(
            ProductRequirement(
                requirement_id=f"requirement-{index}",
                text=f"Requirement {index} revision {revision}",
                acceptance=(f"Acceptance {index}",),
            )
            for index in range(requirement_count)
        ),
        repository_refs=("repo://primary",),
        release_refs=(f"release://{revision}",),
    )


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "semantic.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Semantic continuity qualification",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    package = ResearchEvidencePackage(
        "research-1",
        (EvidenceRef("evidence-1", "research://claim/1", "Evidence-backed option"),),
    )
    projects.record_research_handoff(
        "project-1",
        package,
        (
            ProductOption(
                "option-1",
                "Option one",
                "Evidence-backed product direction",
                ("research-1",),
            ),
        ),
    )
    ProductDecisionRepository(store).record(
        "project-1",
        ProductDecision(
            decision_id="decision-1",
            option_id="option-1",
            state=ProductDecisionState.APPROVED,
            rationale="Selected from recorded research",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:1",
    )
    return store, projects


def _advance(projects: ProductProjectRepository, revision: int, count: int = 6) -> None:
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(revision, count),
        expected_row_version=current.row_version,
        change_reason=f"scope revision {revision}",
    )


def test_semantic_anchor_is_deterministic_compact_and_payload_free(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistorySemanticContinuityService(store)

    first = service.export(generation)
    second = service.export(generation)
    assert first.descriptor_bytes == second.descriptor_bytes
    assert first.descriptor_digest_sha256 == second.descriptor_digest_sha256
    assert service.verify_descriptor(first.descriptor_bytes) == first

    archive = service.generations.segments.reassemble(
        generation.segment_manifest_bytes,
        generation.segment_bytes,
    )
    assert len(first.descriptor_bytes) < len(archive)
    assert b"Evidence-backed product direction" not in first.descriptor_bytes
    assert b"Selected from recorded research" not in first.descriptor_bytes


def test_semantic_window_proves_old_rows_without_cold_archive(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)
    _advance(projects, 2)
    generation_3 = generations.build("project-1", previous=generation_2)

    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generation_1)
    result = service.verify_window(
        anchor.descriptor_bytes,
        (generation_2, generation_3),
        require_live_head=True,
    )

    assert result.verified_generations == (2, 3)
    assert result.head_generation == 3
    assert result.preserved_ordered_records > 0
    assert result.preserved_immutable_records >= 3


def test_semantic_anchor_tampering_fails_closed(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generation)

    envelope = json.loads(anchor.descriptor_bytes)
    envelope["payload"]["row_version"] += 1
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ProductProjectError, match="digest mismatch"):
        service.verify_descriptor(raw)


def test_semantic_proof_rejects_rewritten_ordered_and_identity_history(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)
    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generation_1)
    history = service._history_for_generation(generation_2)

    rewritten_spec = {key: value for key, value in history.items()}
    rewritten_spec["specs"] = [dict(item) for item in history["specs"]]
    rewritten_spec["specs"][0]["created_at"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ProductProjectError, match="rewrote prior specs"):
        service._verify_ordered(anchor, rewritten_spec)

    rewritten_decision = {key: value for key, value in history.items()}
    rewritten_decision["decisions"] = [dict(item) for item in history["decisions"]]
    rewritten_decision["decisions"][0]["rationale"] = "forged rationale"
    with pytest.raises(ProductProjectError, match="rewrote prior decisions"):
        service._verify_immutable(anchor, rewritten_decision)


def test_advance_rejects_stale_live_head(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)
    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generation_1)

    advanced = service.advance(anchor.descriptor_bytes, (generation_2,))
    assert advanced.generation == 2

    _advance(projects, 2)
    with pytest.raises(StaleProjectVersionError):
        service.advance(anchor.descriptor_bytes, (generation_2,))


def test_long_horizon_semantic_anchor_survives_restarts_and_many_records(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generation_service = ProductProjectHistoryGenerationService(store)
    generations = []
    previous = None

    for revision in range(1, 121):
        _advance(projects, revision, count=160)
        if revision % 20 == 0:
            previous = generation_service.build(
                "project-1",
                previous=previous,
                target_entries_per_segment=23,
            )
            generations.append(previous)
            store = SQLiteStore(store.path)
            store.initialize()
            projects = ProductProjectRepository(store)
            generation_service = ProductProjectHistoryGenerationService(store)

    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generations[2])
    result = service.verify_window(
        anchor.descriptor_bytes,
        tuple(generations[3:]),
        require_live_head=True,
    )

    assert anchor.generation == 3
    assert result.verified_generations == (4, 5, 6)
    assert result.head_spec_version == 121
    assert result.head_row_version == 121
    assert result.preserved_ordered_records >= 120
    assert result.preserved_immutable_records >= 4
