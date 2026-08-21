from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)
from nika_core.product_project_history_generations import ProductProjectHistoryGenerationService
from nika_core.product_project_history_semantic_continuity import (
    ProductProjectHistorySemanticContinuityService,
)
from nika_core.product_project_history_sharded_commitments import (
    ProductProjectHistoryShardedCommitmentService,
)


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "shards.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Sharded history qualification",
        spec=ProductProjectSpec(
            goal="Build a generic durable product",
            desired_outcome="Bounded PF12 commitment transport",
        ),
        idempotency_key="create:project-1",
    )
    return store, projects


def _research(projects: ProductProjectRepository, count: int, *, start: int = 0) -> None:
    for index in range(start, start + count):
        package_id = f"research-{index:05d}"
        projects.record_research_handoff(
            "project-1",
            ResearchEvidencePackage(
                package_id,
                (
                    EvidenceRef(
                        f"evidence-{index:05d}",
                        f"research://claim/{index:05d}",
                        f"Claim {index}",
                    ),
                ),
            ),
            (
                ProductOption(
                    f"option-{index:05d}",
                    f"Option {index}",
                    f"Summary {index}",
                    (package_id,),
                ),
            ),
        )


def test_commitment_index_is_deterministic_and_bounded(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 700)
    generation = ProductProjectHistoryGenerationService(store).build(
        "project-1",
        target_entries_per_segment=29,
    )
    service = ProductProjectHistoryShardedCommitmentService(store)

    first = service.export(generation, target_records_per_shard=64)
    second = service.export(generation, target_records_per_shard=64)
    semantic = ProductProjectHistorySemanticContinuityService(store).export(generation)

    assert first.descriptor_bytes == second.descriptor_bytes
    assert first.shard_bytes == second.shard_bytes
    assert service.verify_index(first.descriptor_bytes).shards == first.shards
    assert first.total_immutable_records >= 701
    assert len(first.shards) >= 11
    assert all(item.record_count <= 64 for item in first.shards)
    assert len(first.descriptor_bytes) < len(semantic.descriptor_bytes) // 3
    assert b"Claim 0" not in first.descriptor_bytes
    assert b"Summary 0" not in first.descriptor_bytes


def test_selected_shards_prove_preservation_without_cold_archive_payload(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 300)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1", target_entries_per_segment=17)
    index = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=40,
    )

    _research(projects, 120, start=300)
    generation_2 = generations.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=17,
    )
    store = SQLiteStore(store.path)
    store.initialize()
    service = ProductProjectHistoryShardedCommitmentService(store)

    selected = (index.shard_bytes[0], index.shard_bytes[len(index.shard_bytes) // 2])
    result = service.verify_selected_shards(
        index.descriptor_bytes,
        selected,
        (generation_2,),
    )
    assert result.project_id == "project-1"
    assert result.anchor_generation == 1
    assert result.head_generation == 2
    assert len(result.verified_shards) == 2
    assert result.verified_records > 0


def test_index_and_shard_tampering_fail_closed(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 80)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryShardedCommitmentService(store)
    exported = service.export(generation, target_records_per_shard=16)
    _research(projects, 1, start=80)
    descendant = ProductProjectHistoryGenerationService(store).build(
        "project-1", previous=generation
    )

    descriptor = json.loads(exported.descriptor_bytes)
    descriptor["payload"]["generation"] += 1
    tampered_descriptor = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with pytest.raises(ProductProjectError, match="index digest mismatch"):
        service.verify_index(tampered_descriptor)

    shard = json.loads(exported.shard_bytes[0])
    shard["payload"]["records"][0]["row_digest_sha256"] = "0" * 64
    tampered_shard = json.dumps(shard, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    with pytest.raises(ProductProjectError, match="shard digest mismatch"):
        service.verify_selected_shards(
            exported.descriptor_bytes,
            (tampered_shard,),
            (descendant,),
        )


def test_rehashed_shard_not_declared_by_index_is_rejected(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 80)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryShardedCommitmentService(store)
    exported = service.export(generation, target_records_per_shard=16)
    _research(projects, 1, start=80)
    descendant = ProductProjectHistoryGenerationService(store).build(
        "project-1", previous=generation
    )

    shard = json.loads(exported.shard_bytes[0])
    shard["payload"]["shard_index"] = 999
    payload = shard["payload"]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    shard["digest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    raw = json.dumps(shard, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ProductProjectError):
        service.verify_selected_shards(exported.descriptor_bytes, (raw,), (descendant,))


def test_duplicate_and_undeclared_shards_fail_closed(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 100)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryShardedCommitmentService(store)
    exported = service.export(generation, target_records_per_shard=20)
    _research(projects, 1, start=100)
    descendant = ProductProjectHistoryGenerationService(store).build(
        "project-1", previous=generation
    )

    with pytest.raises(ProductProjectError, match="duplicate commitment shard"):
        service.verify_selected_shards(
            exported.descriptor_bytes,
            (exported.shard_bytes[0], exported.shard_bytes[0]),
            (descendant,),
        )
    with pytest.raises(ProductProjectError, match="no commitment shards"):
        service.verify_selected_shards(exported.descriptor_bytes, (), (descendant,))


def test_large_commitment_set_survives_restart_and_selective_verification(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 1200)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1", target_entries_per_segment=37)
    service = ProductProjectHistoryShardedCommitmentService(store)
    index = service.export(generation_1, target_records_per_shard=128)

    _research(projects, 300, start=1200)
    generation_2 = generations.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=37,
    )
    store = SQLiteStore(store.path)
    store.initialize()
    service = ProductProjectHistoryShardedCommitmentService(store)

    selected = tuple(index.shard_bytes[position] for position in (0, 5, 10, 15))
    result = service.verify_selected_shards(
        index.descriptor_bytes,
        selected,
        (generation_2,),
    )
    assert index.total_immutable_records >= 1201
    assert len(index.shards) >= 19
    assert result.verified_records == sum(
        index.shards[position].record_count for position in (0, 5, 10, 15)
    )
