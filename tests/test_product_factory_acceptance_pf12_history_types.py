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
)
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService
from nika_core.product_project_history_chain_heads import (
    ProductProjectHistoryChainHeadService,
)
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGenerationService,
)
from nika_core.product_project_history_semantic_continuity import (
    ProductProjectHistorySemanticContinuityService,
)
from nika_core.product_project_history_sharded_commitments import (
    ProductProjectHistoryShardedCommitmentService,
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _rehashed_boolean(
    raw: bytes,
    path: tuple[str, ...],
) -> tuple[bytes, str]:
    """Replace one JSON integer with true and honestly rehash the envelope."""
    envelope = json.loads(raw)
    payload = envelope["payload"]
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = True
    digest = _digest(payload)
    envelope["digest_sha256"] = digest
    return _canonical_bytes(envelope), digest


def _project(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "pf12-history-types.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="project-1",
        name="PF12 strict history typing",
        spec=ProductProjectSpec(
            goal="Build a durable generic product",
            desired_outcome="Strict restart-history identity",
        ),
        idempotency_key="create:project-1",
    )
    return store


def test_pf12_archive_rejects_rehashed_boolean_current_spec_version(tmp_path) -> None:
    store = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    forged, _ = _rehashed_boolean(
        archive.bytes,
        ("history", "project", "current_spec_version"),
    )

    with pytest.raises(ProductProjectError):
        service.verify(forged)


def test_pf12_generation_manifest_rejects_rehashed_boolean_generation(tmp_path) -> None:
    store = _project(tmp_path)
    service = ProductProjectHistoryGenerationService(store)
    generation = service.build("project-1")
    forged_bytes, forged_digest = _rehashed_boolean(
        generation.generation_manifest_bytes,
        ("generation",),
    )
    forged = replace(
        generation,
        generation_manifest_digest_sha256=forged_digest,
        generation_manifest_bytes=forged_bytes,
    )

    with pytest.raises(ProductProjectError):
        service.verify(forged)


def test_pf12_chain_head_rejects_rehashed_boolean_generation(tmp_path) -> None:
    store = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryChainHeadService(store)
    descriptor = service.export(generation)
    forged, _ = _rehashed_boolean(descriptor.descriptor_bytes, ("generation",))

    with pytest.raises(ProductProjectError):
        service.verify_descriptor(forged)


def test_pf12_semantic_anchor_rejects_rehashed_boolean_generation(tmp_path) -> None:
    store = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistorySemanticContinuityService(store)
    descriptor = service.export(generation)
    forged, _ = _rehashed_boolean(descriptor.descriptor_bytes, ("generation",))

    with pytest.raises(ProductProjectError):
        service.verify_descriptor(forged)


def test_pf12_v1_shard_index_rejects_rehashed_boolean_generation(tmp_path) -> None:
    store = _project(tmp_path)
    generation = ProductProjectHistoryGenerationService(store).build("project-1")
    service = ProductProjectHistoryShardedCommitmentService(store)
    descriptor = service.export(generation, target_records_per_shard=16)
    forged, _ = _rehashed_boolean(descriptor.descriptor_bytes, ("generation",))

    with pytest.raises(ProductProjectError):
        service.verify_index(forged)
