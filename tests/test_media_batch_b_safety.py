from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    MediaResourceClaim,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ModelDescriptor,
    ProcessingJob,
    ResourceClass,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.repository import MediaRepository
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.media.schema import initialize_media_schema
from nika_core.media.transcribers import FasterWhisperTranscriber, SherpaOnnxWhisperTranscriber
from nika_core.media.transcription import ChunkState, plan_chunks
from nika_core.media.transcription_repository import TranscriptionChunkRepository
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class BusyObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=1,
            memory_percent=1,
            available_memory_bytes=8_000_000_000,
        )


def _build_job(tmp_path: Path) -> tuple[SQLiteStore, ProcessingJob]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    repo = MediaRepository(store)
    source = MediaSource(
        source_id="source",
        kind=MediaSourceKind.LOCAL_FILE,
        locator="input.wav",
    )
    version = MediaVersion(
        version_id="version",
        source_id=source.source_id,
        metadata_sha256="1" * 64,
        duration_seconds=1,
    )
    job = ProcessingJob(
        job_id="job",
        source_id=source.source_id,
        version_id=version.version_id,
        stage="transcription",
    )
    repo.put_source(source)
    repo.put_version(version)
    repo.put_job(job)
    return store, job


def test_cancelled_chunk_is_not_returned_as_restart_work(tmp_path: Path) -> None:
    store, job = _build_job(tmp_path)
    repository = TranscriptionChunkRepository(store)
    chunks = plan_chunks(job_id=job.job_id, duration_ms=1000)
    repository.put(chunks[0].model_copy(update={"state": ChunkState.CANCELLED}))

    assert repository.pending_for_resume(job.job_id) == ()
    assert repository.get(chunks[0].chunk_id).state == ChunkState.CANCELLED


def test_faster_whisper_missing_model_is_typed_and_does_not_download(tmp_path: Path) -> None:
    model = ModelDescriptor(
        model_id="fw-model",
        engine_id="faster-whisper",
        version="test",
        license_reference="explicit-model-license-required",
    )
    with pytest.raises(MediaError) as caught:
        FasterWhisperTranscriber(model_path=tmp_path / "missing-model", model=model)
    assert caught.value.code == MediaErrorCode.COMPONENT_MISSING
    assert not (tmp_path / "missing-model").exists()


def test_sherpa_missing_model_files_are_typed_and_not_created(tmp_path: Path) -> None:
    model = ModelDescriptor(
        model_id="sherpa-model",
        engine_id="sherpa-onnx",
        version="test",
        license_reference="explicit-model-license-required",
    )
    encoder = tmp_path / "encoder.onnx"
    decoder = tmp_path / "decoder.onnx"
    tokens = tmp_path / "tokens.txt"
    with pytest.raises(MediaError) as caught:
        SherpaOnnxWhisperTranscriber(
            encoder=encoder,
            decoder=decoder,
            tokens=tokens,
            model=model,
        )
    assert caught.value.code == MediaErrorCode.COMPONENT_MISSING
    assert not encoder.exists()
    assert not decoder.exists()
    assert not tokens.exists()


def test_denied_heavy_model_claim_does_not_leave_stale_fifo_entry(tmp_path: Path) -> None:
    store, _job = _build_job(tmp_path)
    manager = ResourceManager(store, BusyObserver())
    coordinator = MediaResourceCoordinator(manager)
    first = MediaResourceClaim(
        claim_id="first",
        owner_id="job-1",
        resource_class=ResourceClass.HEAVY_MODEL,
        max_concurrent=1,
    )
    second = MediaResourceClaim(
        claim_id="second",
        owner_id="job-2",
        resource_class=ResourceClass.HEAVY_MODEL,
        max_concurrent=1,
    )
    lease = coordinator.request(first)
    with pytest.raises(MediaError) as caught:
        coordinator.request(second)
    assert caught.value.code == MediaErrorCode.RESOURCE_BLOCKED
    assert manager.queued(scope="media_heavy_model", owner_id="local_machine") == ()
    assert coordinator.release(lease)
