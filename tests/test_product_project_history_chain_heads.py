from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    StaleProjectVersionError,
)
from nika_core.product_project_history_chain_heads import (
    ProductProjectHistoryChainHeadService,
)
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGenerationService,
)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _spec(revision: int = 0, requirement_count: int = 4) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a generic long-lived product",
        desired_outcome="Portable PF12 checkpoint continuity",
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


def _project(tmp_path, project_id: str = "project-1"):
    store = SQLiteStore(tmp_path / f"{project_id}.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id=project_id,
        name="Chain-head qualification",
        spec=_spec(),
        idempotency_key=f"create:{project_id}",
    )
    return store, projects


def _advance_spec(projects: ProductProjectRepository, revision: int) -> None:
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(revision),
        expected_row_version=current.row_version,
        change_reason=f"revision {revision}",
    )


def test_chain_head_descriptor_is_deterministic_and_tamper_evident(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryChainHeadService(store)

    first = service.export(generation)
    second = service.export(generation)
    assert first.descriptor_bytes == second.descriptor_bytes
    assert first.descriptor_digest_sha256 == second.descriptor_digest_sha256
    assert service.verify_descriptor(first.descriptor_bytes) == first

    envelope = json.loads(first.descriptor_bytes)
    envelope["payload"]["row_version"] += 1
    tampered = _canonical(envelope).encode("utf-8")
    with pytest.raises(ProductProjectError, match="digest mismatch"):
        service.verify_descriptor(tampered)


def test_partial_window_verifies_without_loading_older_cold_generations(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance_spec(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)
    _advance_spec(projects, 2)
    generation_3 = generations.build("project-1", previous=generation_2)
    _advance_spec(projects, 3)
    generation_4 = generations.build("project-1", previous=generation_3)

    service = ProductProjectHistoryChainHeadService(store)
    trusted_anchor = service.export(generation_2)
    result = service.verify_window(
        trusted_anchor.descriptor_bytes,
        (generation_3, generation_4),
        require_live_head=True,
    )

    assert result.trusted_anchor.generation == 2
    assert [item.generation for item in result.verified_generations] == [3, 4]
    assert result.head_generation == 4
    assert result.head_archive_digest_sha256 == generation_4.archive_digest_sha256


def test_partial_window_rejects_skip_wrong_project_and_anchor_replay(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance_spec(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)
    _advance_spec(projects, 2)
    generation_3 = generations.build("project-1", previous=generation_2)
    _advance_spec(projects, 3)
    generation_4 = generations.build("project-1", previous=generation_3)

    service = ProductProjectHistoryChainHeadService(store)
    anchor = service.export(generation_2)
    with pytest.raises(ProductProjectError, match="continue anchor generation"):
        service.verify_window(anchor.descriptor_bytes, (generation_4,))
    with pytest.raises(ProductProjectError, match="continue anchor generation"):
        service.verify_window(anchor.descriptor_bytes, (generation_2, generation_3))

    other_store, _ = _project(tmp_path, project_id="project-2")
    other_generation = ProductProjectHistoryGenerationService(other_store).build("project-2")
    other_anchor = ProductProjectHistoryChainHeadService(other_store).export(other_generation)
    with pytest.raises(ProductProjectError, match="project mismatch"):
        service.verify_window(other_anchor.descriptor_bytes, (generation_2, generation_3))


def test_partial_window_rejects_broken_anchor_link_even_if_manifest_is_rehashed(
    tmp_path,
) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance_spec(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)

    envelope = json.loads(generation_2.generation_manifest_bytes)
    payload = envelope["payload"]
    payload["previous_generation_manifest_digest_sha256"] = "f" * 64
    manifest_digest = _digest(payload)
    forged = replace(
        generation_2,
        generation_manifest_digest_sha256=manifest_digest,
        generation_manifest_bytes=_canonical(
            {"digest_sha256": manifest_digest, "payload": payload}
        ).encode("utf-8"),
    )
    generations.verify(forged)

    service = ProductProjectHistoryChainHeadService(store)
    anchor = service.export(generation_1)
    with pytest.raises(ProductProjectError, match="predecessor digest mismatch"):
        service.verify_window(anchor.descriptor_bytes, (forged,))


def test_advance_requires_current_live_head_and_rejects_stale_checkpoint(tmp_path) -> None:
    store, projects = _project(tmp_path)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    _advance_spec(projects, 1)
    generation_2 = generations.build("project-1", previous=generation_1)

    service = ProductProjectHistoryChainHeadService(store)
    anchor = service.export(generation_1)
    advanced = service.advance(anchor.descriptor_bytes, (generation_2,))
    assert advanced.generation == 2
    assert advanced.generation_manifest_digest_sha256 == (
        generation_2.generation_manifest_digest_sha256
    )

    _advance_spec(projects, 2)
    with pytest.raises(StaleProjectVersionError):
        service.advance(anchor.descriptor_bytes, (generation_2,))


def test_long_horizon_partial_window_survives_restart_without_full_cold_chain(
    tmp_path,
) -> None:
    store, projects = _project(tmp_path)
    generation_service = ProductProjectHistoryGenerationService(store)
    generations = []
    previous = None

    for revision in range(1, 61):
        current = projects.get("project-1")
        projects.update_spec(
            "project-1",
            _spec(revision, requirement_count=80),
            expected_row_version=current.row_version,
            change_reason=f"long horizon revision {revision}",
        )
        if revision % 10 == 0:
            previous = generation_service.build(
                "project-1",
                previous=previous,
                target_entries_per_segment=19,
            )
            generations.append(previous)
            store = SQLiteStore(store.path)
            store.initialize()
            projects = ProductProjectRepository(store)
            generation_service = ProductProjectHistoryGenerationService(store)

    service = ProductProjectHistoryChainHeadService(store)
    anchor = service.export(generations[2])
    hot_window = tuple(generations[3:])
    result = service.verify_window(
        anchor.descriptor_bytes,
        hot_window,
        require_live_head=True,
    )

    assert anchor.generation == 3
    assert [item.generation for item in result.verified_generations] == [4, 5, 6]
    assert result.head_spec_version == 61
    assert result.head_row_version == 60
    assert service.advance(anchor.descriptor_bytes, hot_window).generation == 6
