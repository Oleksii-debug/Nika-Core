from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGenerationRetentionPolicy,
    ProductProjectHistoryGenerationService,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _spec(index: int = 0, requirement_count: int = 3) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a generic long-lived product",
        desired_outcome="Verifiable multi-generation PF1 history",
        requirements=tuple(
            ProductRequirement(
                requirement_id=f"requirement-{item}",
                text=f"Requirement {item} at revision {index}",
                acceptance=(f"Acceptance {item}",),
            )
            for item in range(requirement_count)
        ),
        repository_refs=("repo://primary",),
        release_refs=(f"release://{index}",),
    )


def _project(tmp_path) -> tuple[SQLiteStore, ProductProjectRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Generation qualification",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    return store, projects


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def test_generation_chain_survives_restart_and_rejects_stale_live_head(tmp_path) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation_1 = service.build("project-1", target_entries_per_segment=4)

    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(1),
        expected_row_version=current.row_version,
        change_reason="revision one",
    )
    generation_2 = service.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=4,
    )

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    restarted_service = ProductProjectHistoryGenerationService(restarted)
    summaries = restarted_service.verify_chain_against_live((generation_1, generation_2))
    assert [item.generation for item in summaries] == [1, 2]
    assert summaries[-1].spec_version == 2

    restarted_projects = ProductProjectRepository(restarted)
    current = restarted_projects.get("project-1")
    restarted_projects.update_spec(
        "project-1",
        _spec(2),
        expected_row_version=current.row_version,
        change_reason="revision after checkpoint",
    )
    with pytest.raises(StaleProjectVersionError):
        restarted_service.verify_chain_against_live((generation_1, generation_2))


def test_generation_chain_rejects_fork_and_missing_generation(tmp_path) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation_1 = service.build("project-1", target_entries_per_segment=3)

    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(1),
        expected_row_version=current.row_version,
        change_reason="branch point",
    )
    generation_2a = service.build("project-1", previous=generation_1)

    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(2),
        expected_row_version=current.row_version,
        change_reason="alternate descendant",
    )
    generation_2b = service.build("project-1", previous=generation_1)
    generation_3 = service.build("project-1", previous=generation_2a)

    with pytest.raises(ProductProjectError, match="sequence|predecessor"):
        service.verify_chain((generation_1, generation_2a, generation_2b))
    with pytest.raises(ProductProjectError, match="sequence|predecessor"):
        service.verify_chain((generation_1, generation_3))


def test_generation_chain_rejects_replayed_old_archive_even_if_manifest_is_rehashed(
    tmp_path,
) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation_1 = service.build("project-1")
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(1),
        expected_row_version=current.row_version,
        change_reason="advance",
    )
    generation_2 = service.build("project-1", previous=generation_1)

    envelope = json.loads(generation_2.generation_manifest_bytes)
    payload = envelope["payload"]
    payload["generation"] = 3
    payload["previous_generation_manifest_digest_sha256"] = (
        generation_2.generation_manifest_digest_sha256
    )
    payload["previous_archive_digest_sha256"] = generation_2.archive_digest_sha256
    digest = _digest(payload)
    replayed = replace(
        generation_2,
        generation=3,
        generation_manifest_digest_sha256=digest,
        generation_manifest_bytes=_canonical(
            {"digest_sha256": digest, "payload": payload}
        ).encode("utf-8"),
    )

    service.verify(replayed)
    with pytest.raises(ProductProjectError, match="replayed an old head"):
        service.verify_chain((generation_1, generation_2, replayed))


def test_research_only_history_growth_can_create_next_generation_without_row_change(
    tmp_path,
) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation_1 = service.build("project-1")

    projects.record_research_handoff(
        "project-1",
        ResearchEvidencePackage(
            "research-1",
            (EvidenceRef("evidence-1", "research://claim/1", "Observed constraint"),),
        ),
        (
            ProductOption(
                "option-1",
                "Evidence-backed option",
                "Generic product direction",
                ("research-1",),
            ),
        ),
    )
    generation_2 = service.build("project-1", previous=generation_1)

    assert generation_2.spec_version == generation_1.spec_version
    assert generation_2.row_version == generation_1.row_version
    assert generation_2.archive_digest_sha256 != generation_1.archive_digest_sha256
    service.verify_chain_against_live((generation_1, generation_2))


def test_generation_retention_is_non_destructive(tmp_path) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generations = [service.build("project-1")]
    for index in range(1, 8):
        current = projects.get("project-1")
        projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"revision {index}",
        )
        generations.append(service.build("project-1", previous=generations[-1]))

    plan = service.plan_retention(
        generations,
        ProductProjectHistoryGenerationRetentionPolicy(hot_generation_count=3),
    )
    assert plan.hot_generations == (6, 7, 8)
    assert plan.cold_generations == (1, 2, 3, 4, 5)
    assert plan.destructive_delete_allowed is False
    with pytest.raises(ProductProjectError, match="destructive deletion"):
        ProductProjectHistoryGenerationRetentionPolicy(
            hot_generation_count=1,
            allow_destructive_delete=True,
        )


def test_long_horizon_mixed_history_across_many_checkpoint_generations(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = []
    previous = None
    for index in range(1, 41):
        current = projects.get("project-1")
        current = projects.update_spec(
            "project-1",
            _spec(index, requirement_count=120),
            expected_row_version=current.row_version,
            change_reason=f"long horizon revision {index}",
        )
        if index % 5 == 0:
            lifecycle = ProductProjectLifecycleService(store)
            paused = lifecycle.transition(
                "project-1",
                ProductProjectState.PAUSED,
                expected_row_version=current.row_version,
                idempotency_key=f"pause:{index}",
                reason="checkpoint maintenance pause",
                changed_by_ref="user://owner",
            )
            lifecycle.transition(
                "project-1",
                ProductProjectState.ACTIVE,
                expected_row_version=paused.row_version,
                idempotency_key=f"resume:{index}",
                reason="resume after checkpoint",
                changed_by_ref="user://owner",
            )
        if index % 10 == 0:
            service = ProductProjectHistoryGenerationService(store)
            previous = service.build(
                "project-1",
                previous=previous,
                target_entries_per_segment=23,
            )
            generations.append(previous)
            store = SQLiteStore(store.path)
            store.initialize()
            projects = ProductProjectRepository(store)

    service = ProductProjectHistoryGenerationService(store)
    summaries = service.verify_chain_against_live(generations)
    assert len(summaries) == 4
    assert summaries[-1].spec_version == 41
    assert summaries[-1].row_version == 56
    assert all(
        left.archive_digest_sha256 != right.archive_digest_sha256
        for left, right in zip(summaries, summaries[1:])
    )


def test_generation_build_rejects_cross_project_predecessor_and_stale_expected_version(
    tmp_path,
) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation_1 = service.build("project-1")
    projects.create(
        project_id="project-2",
        name="Second project",
        spec=_spec(),
        idempotency_key="create:project-2",
    )
    with pytest.raises(ProductProjectError, match="project mismatch"):
        service.build("project-2", previous=generation_1)
    with pytest.raises(StaleProjectVersionError):
        service.build("project-1", expected_row_version=999)
