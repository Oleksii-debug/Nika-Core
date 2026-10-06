from __future__ import annotations

import wave
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    EngineDescriptor,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ModelDescriptor,
    ProcessingJob,
    Segment,
)
from nika_core.media.files import PromotedFile
from nika_core.media.hashing import sha256_file
from nika_core.media.repository import MediaRepository
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.media.schema import initialize_media_schema
from nika_core.media.transcription import TranscriptionResult
from nika_core.media.transcription_coordinator import TranscriptionCoordinator
from nika_core.media.transcription_repository import TranscriptionChunkRepository
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(cpu_percent=1, memory_percent=1, available_memory_bytes=8_000_000_000)


class Extractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, *, destination_path, **kwargs) -> PromotedFile:
        del kwargs
        self.calls += 1
        with wave.open(str(destination_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes((200).to_bytes(2, "little", signed=True) * 1600)
        return PromotedFile(
            path=destination_path,
            sha256=sha256_file(destination_path),
            size_bytes=destination_path.stat().st_size,
        )


class FailOnceTranscriber:
    def __init__(self) -> None:
        self.calls = 0
        self.engine = EngineDescriptor(
            engine_id="fake",
            name="Fake",
            version="1",
            license_id="test-only",
            source_reference="test",
        )
        self.model = ModelDescriptor(
            model_id="fake-model",
            engine_id="fake",
            version="1",
            license_reference="test-only",
            sha256="3" * 64,
        )

    def transcribe(self, request) -> TranscriptionResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated ASR failure")
        return TranscriptionResult(
            chunk_id=request.chunk_id,
            segments=(
                Segment(
                    segment_id="result",
                    start_ms=request.offset_ms,
                    end_ms=request.offset_ms + 100,
                    text="ok",
                ),
            ),
            engine=self.engine,
            model=self.model,
            elapsed_seconds=0.01,
        )


def test_retry_reuses_checksummed_chunk_audio_after_asr_failure(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    media = MediaRepository(store)
    source = MediaSource(source_id="source", kind=MediaSourceKind.LOCAL_FILE, locator="source.mp4")
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
    media.put_source(source)
    media.put_version(version)
    media.put_job(job)
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source")
    chunks = TranscriptionChunkRepository(store)
    extractor = Extractor()
    transcriber = FailOnceTranscriber()
    coordinator = TranscriptionCoordinator(
        chunks=chunks,
        extractor=extractor,
        resources=MediaResourceCoordinator(ResourceManager(store, Observer())),
    )

    with pytest.raises(RuntimeError, match="simulated ASR failure"):
        coordinator.run(
            job=job,
            source_path=source_path,
            work_root=tmp_path,
            duration_ms=1000,
            transcriber=transcriber,
        )
    failed = chunks.list_for_job(job.job_id)[0]
    assert failed.audio_sha256 is not None
    assert extractor.calls == 1

    result = coordinator.run(
        job=job,
        source_path=source_path,
        work_root=tmp_path,
        duration_ms=1000,
        transcriber=transcriber,
    )
    assert extractor.calls == 1
    assert transcriber.calls == 2
    assert [segment.text for segment in result.transcript.segments] == ["ok"]
