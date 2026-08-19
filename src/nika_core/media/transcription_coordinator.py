from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.audio import FFmpegAudioExtractor, inspect_pcm16_wav
from nika_core.media.contracts import (
    MediaResourceClaim,
    ProcessingJob,
    ResourceClass,
    Transcript,
    TranscriptMethod,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.media.transcription import (
    ChunkPlanPolicy,
    ChunkState,
    OfflineTranscriberPort,
    TranscriptionChunk,
    TranscriptionRequest,
    merge_completed_chunks,
    plan_chunks,
)
from nika_core.media.transcription_repository import TranscriptionChunkRepository


@dataclass(frozen=True, slots=True)
class BatchTranscriptionResult:
    transcript: Transcript
    completed_chunks: int
    silent_chunks: int


class TranscriptionCoordinator:
    """Restart-safe chunk coordinator with one-heavy-model-at-a-time resource ownership."""

    def __init__(
        self,
        *,
        chunks: TranscriptionChunkRepository,
        extractor: FFmpegAudioExtractor,
        resources: MediaResourceCoordinator,
    ) -> None:
        self._chunks = chunks
        self._extractor = extractor
        self._resources = resources

    def run(
        self,
        *,
        job: ProcessingJob,
        source_path: Path,
        work_root: Path,
        duration_ms: int,
        transcriber: OfflineTranscriberPort,
        language: str | None = None,
        chunk_policy: ChunkPlanPolicy | None = None,
        cancel_event: threading.Event | None = None,
        silence_peak_threshold: int = 8,
    ) -> BatchTranscriptionResult:
        if job.version_id is None:
            raise ValueError("transcription job requires version_id")
        root = work_root.resolve(strict=True)
        source = source_path.resolve(strict=True)
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("transcription source must stay inside work_root") from exc

        existing = self._chunks.list_for_job(job.job_id)
        if not existing:
            existing = plan_chunks(job_id=job.job_id, duration_ms=duration_ms, policy=chunk_policy)
            for chunk in existing:
                self._chunks.put(chunk)
        else:
            planned = plan_chunks(job_id=job.job_id, duration_ms=duration_ms, policy=chunk_policy)
            if tuple(
                (item.start_ms, item.end_ms, item.core_start_ms, item.core_end_ms)
                for item in existing
            ) != tuple(
                (item.start_ms, item.end_ms, item.core_start_ms, item.core_end_ms)
                for item in planned
            ):
                raise ValueError("durable transcription chunk plan does not match requested policy")

        self._chunks.pending_for_resume(job.job_id)
        chunk_dir = root / "transcription_chunks" / job.job_id
        chunk_dir.mkdir(parents=True, exist_ok=True)
        silent_chunks = 0
        for durable in self._chunks.list_for_job(job.job_id):
            if durable.state == ChunkState.COMPLETED:
                continue
            if durable.state == ChunkState.CANCELLED:
                raise MediaError(
                    MediaErrorCode.PROCESS_CANCELLED,
                    "transcription has a durably cancelled chunk; explicit reset is required",
                )
            if cancel_event is not None and cancel_event.is_set():
                self._chunks.put(durable.model_copy(update={"state": ChunkState.CANCELLED}))
                raise MediaError(MediaErrorCode.PROCESS_CANCELLED, "media transcription was cancelled")

            running = durable.model_copy(
                update={"state": ChunkState.RUNNING, "error_code": None, "error_message": None}
            )
            self._chunks.put(running)
            chunk_path = chunk_dir / f"chunk-{durable.ordinal:06d}.wav"
            try:
                audio_sha256 = self._prepare_chunk_audio(
                    durable=running,
                    source=source,
                    chunk_path=chunk_path,
                    root=root,
                    cancel_event=cancel_event,
                )
                running = running.model_copy(update={"audio_sha256": audio_sha256})
                self._chunks.put(running)
                inspection = inspect_pcm16_wav(chunk_path)
                segments = ()
                if inspection.is_silent(peak_threshold=silence_peak_threshold):
                    silent_chunks += 1
                else:
                    claim = MediaResourceClaim(
                        claim_id=f"asr:{job.job_id}:{durable.ordinal}",
                        owner_id=job.job_id,
                        resource_class=ResourceClass.HEAVY_MODEL,
                        max_concurrent=1,
                    )
                    lease = self._resources.request(claim)
                    try:
                        result = transcriber.transcribe(
                            TranscriptionRequest(
                                chunk_id=durable.chunk_id,
                                audio_path=chunk_path,
                                offset_ms=durable.start_ms,
                                language=language,
                            )
                        )
                    finally:
                        self._resources.release(lease)
                    segments = result.segments
                completed = running.model_copy(
                    update={
                        "state": ChunkState.COMPLETED,
                        "segments": segments,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                self._chunks.put(completed)
            except Exception as exc:
                current = self._chunks.get(durable.chunk_id)
                if current.state != ChunkState.COMPLETED:
                    cancelled = (
                        isinstance(exc, MediaError)
                        and exc.code == MediaErrorCode.PROCESS_CANCELLED
                    )
                    self._chunks.put(
                        current.model_copy(
                            update={
                                "state": ChunkState.CANCELLED if cancelled else ChunkState.FAILED,
                                "error_code": getattr(
                                    getattr(exc, "code", None),
                                    "value",
                                    "transcription_failed",
                                ),
                                "error_message": str(exc)[:1000],
                            }
                        )
                    )
                raise

        completed_chunks = self._chunks.list_for_job(job.job_id)
        transcript = Transcript(
            transcript_id=f"transcript:{job.job_id}",
            version_id=job.version_id,
            method=TranscriptMethod.OFFLINE_ASR,
            language=language,
            segments=merge_completed_chunks(completed_chunks),
        )
        return BatchTranscriptionResult(
            transcript=transcript,
            completed_chunks=len(completed_chunks),
            silent_chunks=silent_chunks,
        )

    def _prepare_chunk_audio(
        self,
        *,
        durable: TranscriptionChunk,
        source: Path,
        chunk_path: Path,
        root: Path,
        cancel_event: threading.Event | None,
    ) -> str:
        if chunk_path.exists():
            inspection = inspect_pcm16_wav(chunk_path)
            if inspection.is_empty:
                raise ValueError("existing transcription chunk audio is empty")
            checksum = sha256_file(chunk_path)
            if durable.audio_sha256 is not None and checksum != durable.audio_sha256:
                raise MediaError(
                    MediaErrorCode.CHECKSUM_MISMATCH,
                    "durable transcription chunk audio checksum changed",
                )
            return checksum
        promoted = self._extractor.extract(
            source_path=source,
            destination_path=chunk_path,
            allowed_root=root,
            start_ms=durable.start_ms,
            end_ms=durable.end_ms,
            cancel_event=cancel_event,
        )
        return promoted.sha256
