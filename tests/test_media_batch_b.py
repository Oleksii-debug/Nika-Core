from __future__ import annotations

import threading
import wave
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.audio import (
    AudioExtractionPolicy,
    FFmpegAudioExtractor,
    inspect_pcm16_wav,
)
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
from nika_core.media.process import ProcessResult
from nika_core.media.repository import MediaRepository
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.media.schema import MEDIA_SCHEMA_VERSION, initialize_media_schema
from nika_core.media.transcription import (
    ChunkPlanPolicy,
    ChunkState,
    TranscriptionResult,
    merge_completed_chunks,
    plan_chunks,
)
from nika_core.media.transcription_coordinator import TranscriptionCoordinator
from nika_core.media.transcription_repository import TranscriptionChunkRepository
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class RecordingRunner:
    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None
        self.cancel_event: threading.Event | None = None

    def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
        del cwd, timeout_seconds, env
        self.argv = tuple(argv)
        self.cancel_event = cancel_event
        Path(self.argv[-1]).write_bytes(b"RIFF-safe-test")
        return ProcessResult(self.argv, 0, b"", b"", 0.01)


class DummyObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(cpu_percent=1, memory_percent=1, available_memory_bytes=8_000_000_000)


class FakeExtractor:
    def __init__(self, *, silent: bool = False) -> None:
        self.calls = 0
        self.silent = silent

    def extract(
        self,
        *,
        source_path,
        destination_path,
        allowed_root,
        start_ms=None,
        end_ms=None,
        cancel_event=None,
        policy=None,
    ) -> PromotedFile:
        del source_path, allowed_root, start_ms, end_ms, cancel_event, policy
        self.calls += 1
        with wave.open(str(destination_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            sample = 0 if self.silent else 200
            handle.writeframes(sample.to_bytes(2, "little", signed=True) * 1600)
        return PromotedFile(
            path=destination_path,
            sha256=sha256_file(destination_path),
            size_bytes=destination_path.stat().st_size,
        )


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0
        self.engine = EngineDescriptor(
            engine_id="fake-asr",
            name="Fake ASR",
            version="1",
            license_id="test-only",
            source_reference="test",
        )
        self.model = ModelDescriptor(
            model_id="fake-model",
            engine_id="fake-asr",
            version="1",
            license_reference="test-only",
            sha256="2" * 64,
        )

    def transcribe(self, request) -> TranscriptionResult:
        self.calls += 1
        start = request.offset_ms + 2500
        return TranscriptionResult(
            chunk_id=request.chunk_id,
            language=request.language,
            segments=(
                Segment(
                    segment_id=f"{request.chunk_id}:segment",
                    start_ms=start,
                    end_ms=start + 500,
                    text="ok",
                ),
            ),
            engine=self.engine,
            model=self.model,
            elapsed_seconds=0.01,
        )


def build_store(tmp_path: Path) -> tuple[SQLiteStore, MediaRepository, ProcessingJob]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    repo = MediaRepository(store)
    source = MediaSource(
        source_id="source-1",
        kind=MediaSourceKind.LOCAL_FILE,
        locator="input.wav",
    )
    version = MediaVersion(
        version_id="version-1",
        source_id=source.source_id,
        metadata_sha256="1" * 64,
        duration_seconds=65,
    )
    job = ProcessingJob(
        job_id="job-1",
        source_id=source.source_id,
        version_id=version.version_id,
        stage="transcription",
    )
    repo.put_source(source)
    repo.put_version(version)
    repo.put_job(job)
    return store, repo, job


def test_media_schema_batch_b_migration_is_applied(tmp_path: Path) -> None:
    store, _repo, _job = build_store(tmp_path)
    assert MEDIA_SCHEMA_VERSION == 2
    with store.connection() as conn:
        row = conn.execute("SELECT MAX(version) AS version FROM media_schema_migrations").fetchone()
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='media_transcription_chunks'"
        ).fetchone()
    assert int(row["version"]) == 2
    assert table["name"] == "media_transcription_chunks"


def test_chunk_plan_has_deterministic_overlap_and_core_bounds() -> None:
    chunks = plan_chunks(
        job_id="job",
        duration_ms=65_000,
        policy=ChunkPlanPolicy(chunk_ms=30_000, overlap_ms=2_000),
    )
    assert [(item.start_ms, item.end_ms) for item in chunks] == [
        (0, 32_000),
        (28_000, 62_000),
        (58_000, 65_000),
    ]
    assert [(item.core_start_ms, item.core_end_ms) for item in chunks] == [
        (0, 30_000),
        (30_000, 60_000),
        (60_000, 65_000),
    ]


def test_merge_discards_overlap_duplicates_by_core_midpoint() -> None:
    chunks = plan_chunks(
        job_id="job",
        duration_ms=60_000,
        policy=ChunkPlanPolicy(chunk_ms=30_000, overlap_ms=2_000),
    )
    first = chunks[0].model_copy(
        update={
            "state": ChunkState.COMPLETED,
            "segments": (
                Segment(segment_id="a", start_ms=29_000, end_ms=30_500, text="first"),
            ),
        }
    )
    second = chunks[1].model_copy(
        update={
            "state": ChunkState.COMPLETED,
            "segments": (
                Segment(segment_id="duplicate", start_ms=29_000, end_ms=30_500, text="dup"),
                Segment(segment_id="b", start_ms=31_000, end_ms=32_000, text="second"),
            ),
        }
    )
    merged = merge_completed_chunks((first, second))
    assert [item.segment_id for item in merged] == ["a", "b"]


def test_completed_chunks_survive_restart_and_running_chunks_requeue(tmp_path: Path) -> None:
    store, _repo, job = build_store(tmp_path)
    chunks = plan_chunks(job_id=job.job_id, duration_ms=61_000)
    repository = TranscriptionChunkRepository(store)
    completed = chunks[0].model_copy(
        update={
            "state": ChunkState.COMPLETED,
            "segments": (Segment(segment_id="done", start_ms=0, end_ms=1000, text="ok"),),
        }
    )
    running = chunks[1].model_copy(update={"state": ChunkState.RUNNING})
    repository.put(completed)
    repository.put(running)
    repository.put(chunks[2])

    resumable = repository.pending_for_resume(job.job_id)
    assert [item.ordinal for item in resumable] == [1, 2]
    assert repository.get(completed.chunk_id).state == ChunkState.COMPLETED
    assert repository.get(running.chunk_id).state == ChunkState.PENDING
    assert repository.get(running.chunk_id).error_code == "restart_reconciliation_required"


def test_completed_chunk_is_immutable(tmp_path: Path) -> None:
    store, _repo, job = build_store(tmp_path)
    repository = TranscriptionChunkRepository(store)
    chunk = plan_chunks(job_id=job.job_id, duration_ms=1000)[0].model_copy(
        update={"state": ChunkState.COMPLETED}
    )
    repository.put(chunk)
    changed = chunk.model_copy(
        update={
            "segments": (
                Segment(segment_id="late", start_ms=0, end_ms=1, text="changed"),
            )
        }
    )
    with pytest.raises(ValueError, match="immutable"):
        repository.put(changed)


def test_ffmpeg_audio_extraction_uses_fixed_argv_and_atomic_partial(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = root / "відео input.mp4"
    source.write_bytes(b"source")
    ffmpeg = root / "ffmpeg.exe"
    ffmpeg.write_bytes(b"fake")
    runner = RecordingRunner()
    extractor = FFmpegAudioExtractor(ffmpeg_path=ffmpeg, runner=runner)
    destination = root / "normalized audio.wav"
    cancel = threading.Event()

    promoted = extractor.extract(
        source_path=source,
        destination_path=destination,
        allowed_root=root,
        policy=AudioExtractionPolicy(timeout_seconds=2),
        start_ms=1000,
        end_ms=5000,
        cancel_event=cancel,
    )

    assert promoted.path == destination
    assert destination.read_bytes() == b"RIFF-safe-test"
    assert runner.cancel_event is cancel
    assert runner.argv is not None
    assert "-nostdin" in runner.argv
    assert "-map_metadata" in runner.argv
    assert ("-ss", "1.000", "-t", "4.000") == tuple(runner.argv[6:10])
    assert str(source) in runner.argv
    assert runner.argv[-1].endswith(".wav.partial")


def test_pcm16_silence_and_empty_audio_are_explicit(tmp_path: Path) -> None:
    silence = tmp_path / "silence.wav"
    with wave.open(str(silence), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 1600)
    inspection = inspect_pcm16_wav(silence)
    assert inspection.duration_ms == 100
    assert inspection.is_silent()

    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
    assert inspect_pcm16_wav(empty).is_empty


def test_coordinator_skips_completed_chunks_on_second_run(tmp_path: Path) -> None:
    store, _repo, job = build_store(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    chunks = TranscriptionChunkRepository(store)
    extractor = FakeExtractor()
    transcriber = FakeTranscriber()
    coordinator = TranscriptionCoordinator(
        chunks=chunks,
        extractor=extractor,
        resources=MediaResourceCoordinator(ResourceManager(store, DummyObserver())),
    )

    first = coordinator.run(
        job=job,
        source_path=source,
        work_root=tmp_path,
        duration_ms=61_000,
        transcriber=transcriber,
        language="uk",
    )
    assert first.completed_chunks == 3
    assert transcriber.calls == 3
    assert extractor.calls == 3
    assert len(first.transcript.segments) == 3

    second = coordinator.run(
        job=job,
        source_path=source,
        work_root=tmp_path,
        duration_ms=61_000,
        transcriber=transcriber,
        language="uk",
    )
    assert second.transcript == first.transcript
    assert transcriber.calls == 3
    assert extractor.calls == 3


def test_coordinator_does_not_invoke_asr_for_silent_chunks(tmp_path: Path) -> None:
    store, _repo, job = build_store(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    transcriber = FakeTranscriber()
    coordinator = TranscriptionCoordinator(
        chunks=TranscriptionChunkRepository(store),
        extractor=FakeExtractor(silent=True),
        resources=MediaResourceCoordinator(ResourceManager(store, DummyObserver())),
    )
    result = coordinator.run(
        job=job,
        source_path=source,
        work_root=tmp_path,
        duration_ms=1000,
        transcriber=transcriber,
    )
    assert result.silent_chunks == 1
    assert result.transcript.segments == ()
    assert transcriber.calls == 0


def test_ffmpeg_audio_extraction_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"source")
    ffmpeg = root / "ffmpeg.exe"
    ffmpeg.write_bytes(b"fake")
    extractor = FFmpegAudioExtractor(ffmpeg_path=ffmpeg, runner=RecordingRunner())
    with pytest.raises(ValueError, match="allowed_root"):
        extractor.extract(
            source_path=outside,
            destination_path=root / "audio.wav",
            allowed_root=root,
        )
