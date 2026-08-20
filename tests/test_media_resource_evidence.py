from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    EngineDescriptor,
    MediaResourceClaim,
    ModelDescriptor,
    ResourceClass,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.evidence import MediaProofManifest, binary_evidence, model_evidence
from nika_core.media.hashing import sha256_file
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class FakeObserver:
    def __init__(self, available_memory_bytes: int = 8_000_000_000) -> None:
        self.available_memory_bytes = available_memory_bytes

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=self.available_memory_bytes,
        )


def _coordinator(
    tmp_path: Path,
    *,
    available_memory_bytes: int = 8_000_000_000,
    bind_media_observer: bool = True,
) -> tuple[MediaResourceCoordinator, FakeObserver]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    observer = FakeObserver(available_memory_bytes)
    manager = ResourceManager(store, observer)
    return (
        MediaResourceCoordinator(manager, observer if bind_media_observer else None),
        observer,
    )


def _claim(
    claim_id: str,
    resource_class: ResourceClass,
    *,
    owner_id: str = "worker",
    minimum_memory: int | None = None,
    excludes: tuple[ResourceClass, ...] = (),
) -> MediaResourceClaim:
    return MediaResourceClaim(
        claim_id=claim_id,
        owner_id=owner_id,
        resource_class=resource_class,
        min_available_memory_bytes=minimum_memory,
        mutually_exclusive_with=excludes,
    )


def test_media_minimum_memory_is_enforced_before_grant(tmp_path: Path) -> None:
    coordinator, observer = _coordinator(tmp_path, available_memory_bytes=2_000_000_000)
    claim = _claim("asr-1", ResourceClass.HEAVY_MODEL, minimum_memory=4_000_000_000)

    with pytest.raises(MediaError) as caught:
        coordinator.request(claim)
    assert caught.value.code == MediaErrorCode.RESOURCE_BLOCKED
    assert caught.value.retryable is True
    assert "insufficient_available_memory" in str(caught.value)

    observer.available_memory_bytes = 6_000_000_000
    lease = coordinator.request(claim)
    assert lease.scope == "media_heavy_model"
    assert lease.owner_id == "local_machine"
    assert coordinator.release(lease) is True


def test_media_minimum_memory_fails_closed_without_observer(tmp_path: Path) -> None:
    coordinator, _observer = _coordinator(tmp_path, bind_media_observer=False)
    claim = _claim("asr-1", ResourceClass.HEAVY_MODEL, minimum_memory=1)

    with pytest.raises(MediaError, match="cannot be verified") as caught:
        coordinator.request(claim)
    assert caught.value.code == MediaErrorCode.RESOURCE_BLOCKED
    assert caught.value.retryable is True


def test_media_mutual_exclusion_is_symmetric_and_release_restores_access(tmp_path: Path) -> None:
    coordinator, _observer = _coordinator(tmp_path)
    heavy = _claim(
        "heavy-1",
        ResourceClass.HEAVY_MODEL,
        excludes=(ResourceClass.MEDIA_IO,),
    )
    media_io = _claim("io-1", ResourceClass.MEDIA_IO, owner_id="extractor")
    heavy_lease = coordinator.request(heavy)

    with pytest.raises(MediaError, match="mutually_exclusive_resource_class"):
        coordinator.request(media_io)

    assert coordinator.release(heavy_lease) is True
    io_lease = coordinator.request(media_io)
    assert coordinator.release(io_lease) is True

    io_blocks_heavy = _claim(
        "io-2",
        ResourceClass.MEDIA_IO,
        owner_id="extractor",
        excludes=(ResourceClass.HEAVY_MODEL,),
    )
    io_lease = coordinator.request(io_blocks_heavy)
    with pytest.raises(MediaError, match="mutually_exclusive_resource_class"):
        coordinator.request(_claim("heavy-2", ResourceClass.HEAVY_MODEL))
    assert coordinator.release(io_lease) is True


def test_media_claim_id_is_idempotent_but_cannot_change_policy(tmp_path: Path) -> None:
    coordinator, _observer = _coordinator(tmp_path)
    claim = _claim("same", ResourceClass.LIGHT)
    first = coordinator.request(claim)
    second = coordinator.request(claim)
    assert first == second

    changed = _claim("same", ResourceClass.MEDIA_IO)
    with pytest.raises(MediaError, match="different policy") as caught:
        coordinator.request(changed)
    assert caught.value.retryable is False
    assert coordinator.release(first) is True


def test_binary_evidence_hashes_file_without_serializing_local_path(tmp_path: Path) -> None:
    binary = tmp_path / "private path" / "ffprobe.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"proof-binary")

    evidence = binary_evidence(
        component_id="ffprobe",
        path=binary,
        source_reference="https://ffmpeg.org/",
        license_classification="LGPL-2.1-or-later/build-dependent",
    )
    payload = evidence.model_dump_json()
    assert evidence.path_name == "ffprobe.exe"
    assert evidence.sha256 == sha256_file(binary)
    assert str(binary.parent) not in payload


def test_model_evidence_requires_descriptor_checksum_and_size_match(tmp_path: Path) -> None:
    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"model-data")
    descriptor = ModelDescriptor(
        model_id="tessdata-eng",
        engine_id="tesseract",
        version="fixture",
        license_reference="Apache-2.0",
        sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="checksum"):
        model_evidence(
            descriptor=descriptor,
            path=model,
            source_reference="https://github.com/tesseract-ocr/tessdata",
        )

    descriptor = ModelDescriptor(
        model_id="tessdata-eng",
        engine_id="tesseract",
        version="fixture",
        license_reference="Apache-2.0",
        sha256=sha256_file(model),
        size_bytes=model.stat().st_size + 1,
    )
    with pytest.raises(ValueError, match="size"):
        model_evidence(
            descriptor=descriptor,
            path=model,
            source_reference="https://github.com/tesseract-ocr/tessdata",
        )


def test_media_proof_manifest_separates_engine_binary_and_model_identity(tmp_path: Path) -> None:
    binary = tmp_path / "tesseract.exe"
    binary.write_bytes(b"binary")
    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"traineddata")
    engine = EngineDescriptor(
        engine_id="tesseract",
        name="Tesseract OCR",
        version="5.5.2",
        license_id="Apache-2.0",
        source_reference="https://github.com/tesseract-ocr/tesseract",
        executable_sha256=sha256_file(binary),
    )
    descriptor = ModelDescriptor(
        model_id="tessdata-eng",
        engine_id="tesseract",
        version="fixture",
        license_reference="Apache-2.0",
        sha256=sha256_file(model),
        size_bytes=model.stat().st_size,
    )
    manifest = MediaProofManifest(
        engines=(engine,),
        binaries=(
            binary_evidence(
                component_id="tesseract",
                path=binary,
                source_reference="https://github.com/tesseract-ocr/tesseract",
                license_classification="Apache-2.0",
            ),
        ),
        models=(
            model_evidence(
                descriptor=descriptor,
                path=model,
                source_reference="https://github.com/tesseract-ocr/tessdata",
            ),
        ),
    )
    assert manifest.real_engine_execution_proven is False
    assert manifest.target_machine_measured is False
    assert manifest.engines[0].license_id == "Apache-2.0"
    assert manifest.binaries[0].license_classification == "Apache-2.0"
    assert manifest.models[0].license_reference == "Apache-2.0"


def test_media_proof_manifest_rejects_model_without_matching_engine(tmp_path: Path) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    descriptor = ModelDescriptor(
        model_id="orphan",
        engine_id="missing-engine",
        version="1",
        license_reference="test-license",
    )
    evidence = model_evidence(
        descriptor=descriptor,
        path=model,
        source_reference="https://example.invalid/model",
    )
    with pytest.raises(ValueError, match="proven engine"):
        MediaProofManifest(models=(evidence,))
