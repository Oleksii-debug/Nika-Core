from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from nika_core.media.contracts import EngineDescriptor, FrozenModel, ModelDescriptor, Segment
from nika_core.media.errors import MediaError, MediaErrorCode


class ChunkState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TranscriptionChunk(FrozenModel):
    chunk_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=160)
    ordinal: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    core_start_ms: int = Field(ge=0)
    core_end_ms: int = Field(gt=0)
    state: ChunkState = ChunkState.PENDING
    audio_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    segments: tuple[Segment, ...] = ()
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_bounds(self) -> TranscriptionChunk:
        if self.end_ms <= self.start_ms:
            raise ValueError("chunk end_ms must be greater than start_ms")
        if not (self.start_ms <= self.core_start_ms < self.core_end_ms <= self.end_ms):
            raise ValueError("chunk core bounds must lie inside chunk bounds")
        return self


class TranscriptionRequest(FrozenModel):
    chunk_id: str
    audio_path: Path
    offset_ms: int = Field(default=0, ge=0)
    language: str | None = None
    prompt: str | None = Field(default=None, max_length=2000)


class TranscriptionResult(FrozenModel):
    chunk_id: str
    language: str | None = None
    segments: tuple[Segment, ...]
    engine: EngineDescriptor
    model: ModelDescriptor
    elapsed_seconds: float = Field(ge=0)


class OfflineTranscriberPort(Protocol):
    @property
    def engine(self) -> EngineDescriptor: ...

    @property
    def model(self) -> ModelDescriptor: ...

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...


@dataclass(frozen=True, slots=True)
class ChunkPlanPolicy:
    chunk_ms: int = 30_000
    overlap_ms: int = 2_000

    def __post_init__(self) -> None:
        if self.chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        if self.overlap_ms < 0 or self.overlap_ms * 2 >= self.chunk_ms:
            raise ValueError("overlap_ms must be non-negative and less than half chunk_ms")


def plan_chunks(
    *,
    job_id: str,
    duration_ms: int,
    policy: ChunkPlanPolicy | None = None,
) -> tuple[TranscriptionChunk, ...]:
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if duration_ms == 0:
        return ()
    selected = policy or ChunkPlanPolicy()
    chunks: list[TranscriptionChunk] = []
    core_start = 0
    ordinal = 0
    while core_start < duration_ms:
        core_end = min(core_start + selected.chunk_ms, duration_ms)
        start = max(0, core_start - selected.overlap_ms)
        end = min(duration_ms, core_end + selected.overlap_ms)
        chunks.append(
            TranscriptionChunk(
                chunk_id=f"{job_id}:{ordinal:06d}",
                job_id=job_id,
                ordinal=ordinal,
                start_ms=start,
                end_ms=end,
                core_start_ms=core_start,
                core_end_ms=core_end,
            )
        )
        core_start = core_end
        ordinal += 1
    return tuple(chunks)


def merge_completed_chunks(chunks: tuple[TranscriptionChunk, ...]) -> tuple[Segment, ...]:
    if not chunks:
        return ()
    ordered = sorted(chunks, key=lambda item: item.ordinal)
    expected = list(range(len(ordered)))
    if [item.ordinal for item in ordered] != expected:
        raise ValueError("chunk ordinals must be contiguous from zero")
    merged: list[Segment] = []
    for chunk in ordered:
        if chunk.state != ChunkState.COMPLETED:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                f"transcription chunk {chunk.chunk_id} is not completed",
                retryable=True,
            )
        for segment in chunk.segments:
            midpoint = segment.start_ms + (segment.end_ms - segment.start_ms) // 2
            if midpoint < chunk.core_start_ms or midpoint >= chunk.core_end_ms:
                continue
            if merged and segment.start_ms < merged[-1].start_ms:
                raise ValueError("merged transcription segments must remain monotonic")
            merged.append(segment)
    return tuple(merged)
