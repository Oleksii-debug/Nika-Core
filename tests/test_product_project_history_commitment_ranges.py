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
    ResearchEvidencePackage,
)
from nika_core.product_project_history_commitment_ranges import (
    ProductProjectHistoryCommitmentRangeService,
)
from nika_core.product_project_history_generations import ProductProjectHistoryGenerationService
from nika_core.product_project_history_sharded_commitments import (
    ProductProjectHistoryShardedCommitmentService,
)


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "range-proof.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Range proof qualification",
        spec=ProductProjectSpec(
            goal="Build a generic durable product",
            desired_outcome="Compact long-horizon PF12 verification",
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


def _shard_map(raw_shards: tuple[bytes, ...]) -> dict[tuple[str, int], bytes]:
    result: dict[tuple[str, int], bytes] = {}
    for raw in raw_shards:
        payload = json.loads(raw)["payload"]
        result[(payload["section"], payload["shard_index"])] = raw
    return result


def _rehash(envelope: dict) -> bytes:
    payload = envelope["payload"]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    envelope["digest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_v1_upgrade_is_deterministic_compact_and_does_not_rewrite_source(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 700)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation,
        target_records_per_shard=16,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)

    first = service.upgrade_v1(source.descriptor_bytes)
    second = service.upgrade_v1(source.descriptor_bytes)

    assert first.descriptor_bytes == second.descriptor_bytes
    assert first.source_v1_descriptor_digest_sha256 == source.descriptor_digest_sha256
    assert first.total_immutable_records == source.total_immutable_records
    assert tuple(item.section for item in first.sections) == (
        "research_handoffs",
        "decisions",
        "creation_idempotency",
        "mutation_idempotency",
    )
    assert len(first.descriptor_bytes) < len(source.descriptor_bytes) // 4
    assert source.descriptor_bytes == ProductProjectHistoryShardedCommitmentService(
        store
    ).export(generation, target_records_per_shard=16).descriptor_bytes


def test_contiguous_range_proof_verifies_without_full_v1_descriptor(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 320)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1", target_entries_per_segment=19)
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=24,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=3,
        stop_shard_index=7,
    )

    _research(projects, 90, start=320)
    generation_2 = generations.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=19,
    )
    shard_map = _shard_map(source.shard_bytes)
    selected = tuple(shard_map[("research_handoffs", index)] for index in range(3, 7))

    result = service.verify_range_proof(
        compact.descriptor_bytes,
        proof.proof_bytes,
        selected,
        (generation_2,),
    )
    assert result.project_id == "project-1"
    assert result.anchor_generation == 1
    assert result.head_generation == 2
    assert result.verified_shards == 4
    assert result.verified_records == proof.record_count


def test_rehashed_merkle_path_tampering_fails_closed(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 100)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=10,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=2,
        stop_shard_index=3,
    )
    _research(projects, 1, start=100)
    generation_2 = generations.build("project-1", previous=generation_1)

    envelope = json.loads(proof.proof_bytes)
    envelope["payload"]["leaves"][0]["path"][0]["digest_sha256"] = "0" * 64
    tampered = _rehash(envelope)
    shard = _shard_map(source.shard_bytes)[("research_handoffs", 2)]
    with pytest.raises(ProductProjectError, match="Merkle root mismatch"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            tampered,
            (shard,),
            (generation_2,),
        )


def test_range_proof_cannot_omit_or_reorder_declared_shards(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 140)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation,
        target_records_per_shard=10,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=4,
        stop_shard_index=7,
    )
    shards = _shard_map(source.shard_bytes)
    selected = tuple(shards[("research_handoffs", index)] for index in range(4, 7))

    envelope = json.loads(proof.proof_bytes)
    envelope["payload"]["leaves"][1] = envelope["payload"]["leaves"][2]
    omitted = _rehash(envelope)
    with pytest.raises(ProductProjectError, match="not contiguous"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            omitted,
            selected,
            (generation,),
        )

    envelope = json.loads(proof.proof_bytes)
    envelope["payload"]["leaves"][0], envelope["payload"]["leaves"][1] = (
        envelope["payload"]["leaves"][1],
        envelope["payload"]["leaves"][0],
    )
    reordered = _rehash(envelope)
    with pytest.raises(ProductProjectError, match="not contiguous"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            reordered,
            selected,
            (generation,),
        )


def test_wrong_shard_bytes_cannot_satisfy_valid_range_proof(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 120)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=12,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=1,
        stop_shard_index=2,
    )
    _research(projects, 1, start=120)
    generation_2 = generations.build("project-1", previous=generation_1)
    shards = _shard_map(source.shard_bytes)

    with pytest.raises(ProductProjectError, match="shard identity mismatch"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            proof.proof_bytes,
            (shards[("research_handoffs", 2)],),
            (generation_2,),
        )


def test_descendant_must_continue_exact_anchor_generation(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 90)
    generation_1 = ProductProjectHistoryGenerationService(store).build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=15,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=0,
        stop_shard_index=1,
    )
    shard = _shard_map(source.shard_bytes)[("research_handoffs", 0)]

    with pytest.raises(ProductProjectError, match="does not continue"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            proof.proof_bytes,
            (shard,),
            (generation_1,),
        )


def test_empty_section_and_invalid_ranges_fail_closed(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 20)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation,
        target_records_per_shard=8,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)

    with pytest.raises(ProductProjectError, match="interval"):
        service.build_range_proof(
            source.descriptor_bytes,
            section="research_handoffs",
            start_shard_index=1,
            stop_shard_index=1,
        )
    with pytest.raises(ProductProjectError, match="interval"):
        service.build_range_proof(
            source.descriptor_bytes,
            section="decisions",
            start_shard_index=0,
            stop_shard_index=1,
        )
    with pytest.raises(ProductProjectError, match="section"):
        service.build_range_proof(
            source.descriptor_bytes,
            section="unknown",
            start_shard_index=0,
            stop_shard_index=1,
        )


def test_v2_boundaries_reject_boolean_indices_and_versions(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 40)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=10,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=0,
        stop_shard_index=1,
    )

    with pytest.raises(ProductProjectError, match="interval"):
        service.build_range_proof(
            source.descriptor_bytes,
            section="research_handoffs",
            start_shard_index=False,
            stop_shard_index=1,
        )

    compact_envelope = json.loads(compact.descriptor_bytes)
    compact_envelope["payload"]["row_version"] = False
    with pytest.raises(ProductProjectError, match="row_version"):
        service.verify_index(_rehash(compact_envelope))

    proof_envelope = json.loads(proof.proof_bytes)
    proof_envelope["payload"]["start_shard_index"] = False
    with pytest.raises(ProductProjectError, match="start_shard_index"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            _rehash(proof_envelope),
            (),
            (),
        )


def test_v1_upgrade_rejects_boolean_numeric_fields_at_strict_v1_boundary(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 30)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation,
        target_records_per_shard=10,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)

    envelope = json.loads(source.descriptor_bytes)
    envelope["payload"]["generation"] = True
    with pytest.raises(ProductProjectError, match="commitment index generation"):
        service.upgrade_v1(_rehash(envelope))

    envelope = json.loads(source.descriptor_bytes)
    envelope["payload"]["shards"][0]["shard_index"] = False
    with pytest.raises(ProductProjectError, match="commitment shard summary index"):
        service.upgrade_v1(_rehash(envelope))


def test_range_verifier_rejects_boolean_descendant_versions_before_chain_use(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 50)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=10,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=0,
        stop_shard_index=1,
    )
    shard = _shard_map(source.shard_bytes)[("research_handoffs", 0)]

    _research(projects, 1, start=50)
    generation_2 = generations.build("project-1", previous=generation_1)
    forged = replace(generation_2, generation=True)
    with pytest.raises(ProductProjectError, match="descendant generation"):
        service.verify_range_proof(
            compact.descriptor_bytes,
            proof.proof_bytes,
            (shard,),
            (forged,),
        )


def test_large_range_proof_survives_restart_and_live_head_verification(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 1600)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1", target_entries_per_segment=41)
    source = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=32,
    )
    service = ProductProjectHistoryCommitmentRangeService(store)
    compact = service.upgrade_v1(source.descriptor_bytes)
    proof = service.build_range_proof(
        source.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=10,
        stop_shard_index=18,
    )

    _research(projects, 400, start=1600)
    generation_2 = generations.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=41,
    )
    store = SQLiteStore(store.path)
    store.initialize()
    service = ProductProjectHistoryCommitmentRangeService(store)
    shards = _shard_map(source.shard_bytes)
    selected = tuple(
        shards[("research_handoffs", index)]
        for index in range(proof.start_shard_index, proof.stop_shard_index)
    )

    result = service.verify_range_proof(
        compact.descriptor_bytes,
        proof.proof_bytes,
        selected,
        (generation_2,),
        require_live_head=True,
    )
    section = next(item for item in compact.sections if item.section == "research_handoffs")
    assert section.shard_count >= 50
    assert compact.total_immutable_records >= 1601
    assert result.verified_shards == 8
    assert result.verified_records == 8 * 32