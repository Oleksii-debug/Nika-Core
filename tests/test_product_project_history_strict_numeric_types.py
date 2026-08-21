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
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService
from nika_core.product_project_history_chain_heads import ProductProjectHistoryChainHeadService
from nika_core.product_project_history_commitment_ranges import (
    ProductProjectHistoryCommitmentRangeService,
)
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGenerationRetentionPolicy,
    ProductProjectHistoryGenerationService,
)
from nika_core.product_project_history_segments import (
    ProductProjectHistoryRetentionPolicy,
    ProductProjectHistorySegmentService,
)
from nika_core.product_project_history_semantic_continuity import (
    ProductProjectHistorySemanticContinuityService,
)
from nika_core.product_project_history_sharded_commitments import (
    ProductProjectHistoryShardedCommitmentService,
)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rehash(envelope: dict) -> bytes:
    payload = envelope["payload"]
    envelope["digest_sha256"] = hashlib.sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()
    return _canonical(envelope).encode("utf-8")


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "strict-numeric-history.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Strict numeric PF12 qualification",
        spec=ProductProjectSpec(
            goal="Preserve portable ProductProject history",
            desired_outcome="Reject type-confused numeric identities",
        ),
        idempotency_key="create:project-1",
    )
    return store, projects


def _research(projects: ProductProjectRepository, count: int, *, start: int = 0) -> None:
    for index in range(start, start + count):
        package_id = f"research-{index:04d}"
        projects.record_research_handoff(
            "project-1",
            ResearchEvidencePackage(
                package_id,
                (
                    EvidenceRef(
                        f"evidence-{index:04d}",
                        f"research://strict/{index:04d}",
                        f"Claim {index}",
                    ),
                ),
            ),
            (
                ProductOption(
                    f"option-{index:04d}",
                    f"Option {index}",
                    f"Summary {index}",
                    (package_id,),
                ),
            ),
        )


def _replace_segment_digest(manifest_bytes: bytes, digest: str) -> bytes:
    envelope = json.loads(manifest_bytes)
    envelope["payload"]["segments"][0]["digest_sha256"] = digest
    return _rehash(envelope)


def test_archive_versions_require_exact_json_integers(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")

    for bad in (True, "1"):
        envelope = json.loads(archive.bytes)
        envelope["payload"]["spec_version"] = bad
        envelope["payload"]["history"]["project"]["current_spec_version"] = bad
        with pytest.raises(ProductProjectError, match="invalid versions"):
            service.verify(_rehash(envelope))

    for bad in (False, "0"):
        envelope = json.loads(archive.bytes)
        envelope["payload"]["row_version"] = bad
        envelope["payload"]["history"]["project"]["row_version"] = bad
        with pytest.raises(ProductProjectError, match="invalid versions"):
            service.verify(_rehash(envelope))


def test_segment_payload_sequence_and_record_ordinal_reject_booleans(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistorySegmentService(store)
    bundle = service.build("project-1", target_entries_per_segment=1000)
    assert len(bundle.segments) == 1

    envelope = json.loads(bundle.segments[0].bytes)
    envelope["payload"]["sequence"] = True
    bad_segment = _rehash(envelope)
    bad_digest = json.loads(bad_segment)["digest_sha256"]
    bad_manifest = _replace_segment_digest(bundle.manifest_bytes, bad_digest)
    with pytest.raises(ProductProjectError, match="payload sequence"):
        service.verify(bad_manifest, (bad_segment,))

    envelope = json.loads(bundle.segments[0].bytes)
    envelope["payload"]["records"][0]["ordinal"] = False
    bad_segment = _rehash(envelope)
    bad_digest = json.loads(bad_segment)["digest_sha256"]
    bad_manifest = _replace_segment_digest(bundle.manifest_bytes, bad_digest)
    with pytest.raises(ProductProjectError, match="record identity"):
        service.verify(bad_manifest, (bad_segment,))


def test_generation_manifest_and_dataclass_reject_boolean_versions(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation = service.build("project-1")

    envelope = json.loads(generation.generation_manifest_bytes)
    envelope["payload"]["generation"] = True
    bad_manifest = _rehash(envelope)
    forged_manifest = replace(
        generation,
        generation_manifest_bytes=bad_manifest,
        generation_manifest_digest_sha256=json.loads(bad_manifest)["digest_sha256"],
    )
    with pytest.raises(ProductProjectError, match="generation number"):
        service.verify(forged_manifest)

    forged_dataclass = replace(generation, generation=True)
    with pytest.raises(ProductProjectError, match="generation number"):
        service.verify(forged_dataclass)

    forged_row = replace(generation, row_version=False)
    with pytest.raises(ProductProjectError, match="row version"):
        service.verify(forged_row)


def test_chain_head_rejects_boolean_generation_and_versions(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryChainHeadService(store)
    head = service.export(generation)

    for key, bad in (("generation", True), ("spec_version", True), ("row_version", False)):
        envelope = json.loads(head.descriptor_bytes)
        envelope["payload"][key] = bad
        with pytest.raises(ProductProjectError, match="history chain-head"):
            service.verify_descriptor(_rehash(envelope))


def test_semantic_anchor_rejects_boolean_versions_and_counts(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistorySemanticContinuityService(store)
    anchor = service.export(generation)

    envelope = json.loads(anchor.descriptor_bytes)
    envelope["payload"]["generation"] = True
    with pytest.raises(ProductProjectError, match="semantic anchor generation"):
        service.verify_descriptor(_rehash(envelope))

    envelope = json.loads(anchor.descriptor_bytes)
    envelope["payload"]["ordered_sections"][0]["record_count"] = False
    with pytest.raises(ProductProjectError, match="ordered commitment value"):
        service.verify_descriptor(_rehash(envelope))

    envelope = json.loads(anchor.descriptor_bytes)
    envelope["payload"]["immutable_sections"][0]["record_count"] = False
    with pytest.raises(ProductProjectError, match="immutable commitment value"):
        service.verify_descriptor(_rehash(envelope))


def test_sharded_index_and_shards_reject_boolean_numeric_identities(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 6)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1")
    service = ProductProjectHistoryShardedCommitmentService(store)
    index = service.export(generation_1, target_records_per_shard=2)

    for mutate in (
        lambda payload: payload.__setitem__("generation", True),
        lambda payload: payload.__setitem__("target_records_per_shard", True),
        lambda payload: payload["shards"][0].__setitem__("shard_index", False),
        lambda payload: payload["shards"][0].__setitem__("record_count", True),
    ):
        envelope = json.loads(index.descriptor_bytes)
        mutate(envelope["payload"])
        with pytest.raises(ProductProjectError):
            service.verify_index(_rehash(envelope))

    _research(projects, 1, start=6)
    generation_2 = generations.build("project-1", previous=generation_1)
    shard_envelope = json.loads(index.shard_bytes[0])
    shard_envelope["payload"]["generation"] = True
    forged_shard = _rehash(shard_envelope)
    forged_shard_digest = json.loads(forged_shard)["digest_sha256"]
    index_envelope = json.loads(index.descriptor_bytes)
    index_envelope["payload"]["shards"][0]["shard_digest_sha256"] = forged_shard_digest
    forged_index = _rehash(index_envelope)
    with pytest.raises(ProductProjectError, match="shard generation"):
        service.verify_selected_shards(
            forged_index,
            (forged_shard,),
            (generation_2,),
        )


def test_public_size_and_retention_counts_reject_booleans(tmp_path) -> None:
    store, _ = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")

    with pytest.raises(ProductProjectError, match="positive integer"):
        ProductProjectHistorySegmentService(store).build(
            "project-1",
            target_entries_per_segment=True,
        )
    with pytest.raises(ProductProjectError, match="positive integer"):
        ProductProjectHistoryShardedCommitmentService(store).export(
            generation,
            target_records_per_shard=True,
        )
    with pytest.raises(ProductProjectError, match="non-negative integer"):
        ProductProjectHistoryRetentionPolicy(hot_segment_count=False)
    with pytest.raises(ProductProjectError, match="non-negative integer"):
        ProductProjectHistoryGenerationRetentionPolicy(hot_generation_count=True)


def test_strict_history_pipeline_survives_restart_through_range_v2(tmp_path) -> None:
    store, projects = _project(tmp_path)
    _research(projects, 25)
    generations = ProductProjectHistoryGenerationService(store)
    generation_1 = generations.build("project-1", target_entries_per_segment=11)
    chain_head = ProductProjectHistoryChainHeadService(store).export(generation_1)
    semantic = ProductProjectHistorySemanticContinuityService(store).export(generation_1)
    sharded = ProductProjectHistoryShardedCommitmentService(store).export(
        generation_1,
        target_records_per_shard=5,
    )
    ranges = ProductProjectHistoryCommitmentRangeService(store)
    compact = ranges.upgrade_v1(sharded.descriptor_bytes)
    proof = ranges.build_range_proof(
        sharded.descriptor_bytes,
        section="research_handoffs",
        start_shard_index=1,
        stop_shard_index=3,
    )

    _research(projects, 5, start=25)
    generation_2 = generations.build(
        "project-1",
        previous=generation_1,
        target_entries_per_segment=11,
    )

    store = SQLiteStore(store.path)
    store.initialize()
    generations = ProductProjectHistoryGenerationService(store)
    assert generations.verify_chain((generation_1, generation_2))[-1].generation == 2
    assert ProductProjectHistoryChainHeadService(store).verify_window(
        chain_head.descriptor_bytes,
        (generation_2,),
    ).head_generation == 2
    assert ProductProjectHistorySemanticContinuityService(store).verify_window(
        semantic.descriptor_bytes,
        (generation_2,),
    ).head_generation == 2

    selected = tuple(
        raw
        for raw in sharded.shard_bytes
        if json.loads(raw)["payload"]["section"] == "research_handoffs"
        and 1 <= json.loads(raw)["payload"]["shard_index"] < 3
    )
    assert ProductProjectHistoryShardedCommitmentService(store).verify_selected_shards(
        sharded.descriptor_bytes,
        selected,
        (generation_2,),
    ).verified_records == 10
    result = ProductProjectHistoryCommitmentRangeService(store).verify_range_proof(
        compact.descriptor_bytes,
        proof.proof_bytes,
        selected,
        (generation_2,),
        require_live_head=True,
    )
    assert result.head_generation == 2
    assert result.verified_shards == 2
