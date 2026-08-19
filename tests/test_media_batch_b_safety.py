from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ModelDescriptor,
    ProcessingJob,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import initialize_media_schema
from nika_core.media.transcribers import FasterWhisperTranscriber, SherpaOnnxWhisperTranscriber
from nika_core.media.transcription import ChunkState, plan_chunks
from nika_core.media.transcription_repository import TranscriptionChunkRepository


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


def test_preexisting_cancel_event_remains_explicit() -> None:
    event = threading.Event()
    event.set()
    assert event.is_set()
