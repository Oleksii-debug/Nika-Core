from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.audio import AudioExtractionPolicy, FFmpegAudioExtractor
from nika_core.media.contracts import MediaSource, MediaSourceKind, MediaVersion, ProcessingJob, Segment
from nika_core.media.process import ProcessResult
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import MEDIA_SCHEMA_VERSION, initialize_media_schema
from nika_core.media.transcription import (
    ChunkPlanPolicy,
    ChunkState,
    TranscriptionChunk,
    merge_completed_chunks,
    plan_chunks,
)
from nika_core.media.transcription_repository import TranscriptionChunkRepository


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


def build_store(tmp_path: Path) -> tuple[SQLiteStore, MediaRepository, ProcessingJob]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    repo = MediaRepository(store)
    source = MediaSource(source_id="source-1", kind=MediaSourceKind.LOCAL_FILE, locator="input.wav")
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
            "SELECT name FROM sqlite_master WHERE type='table' AND name='media_transcription_chunks'"
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
    with pytest.raises(ValueError, match="immutable"):
        repository.put(chunk.model_copy(update={"segments": (
            Segment(segment_id="late", start_ms=0, end_ms=1, text="changed"),
        )}))


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
        cancel_event=cancel,
    )

    assert promoted.path == destination
    assert destination.read_bytes() == b"RIFF-safe-test"
    assert runner.cancel_event is cancel
    assert runner.argv is not None
    assert "-nostdin" in runner.argv
    assert "-map_metadata" in runner.argv
    assert str(source) in runner.argv
    assert runner.argv[-1].endswith(".wav.partial")


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
