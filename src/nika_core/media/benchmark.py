from __future__ import annotations

import time
from dataclasses import dataclass

from nika_core.media.transcription import OfflineTranscriberPort, TranscriptionRequest


@dataclass(frozen=True, slots=True)
class ASRBenchmarkSample:
    engine_id: str
    model_id: str
    chunk_id: str
    audio_duration_ms: int
    elapsed_seconds: float
    segment_count: int
    text_chars: int

    @property
    def realtime_factor(self) -> float | None:
        if self.audio_duration_ms <= 0:
            return None
        return self.elapsed_seconds / (self.audio_duration_ms / 1000)


def benchmark_transcriber(
    transcriber: OfflineTranscriberPort,
    cases: tuple[tuple[TranscriptionRequest, int], ...],
) -> tuple[ASRBenchmarkSample, ...]:
    """Collect comparable measurements only; deliberately does not select a winner."""
    samples: list[ASRBenchmarkSample] = []
    for request, audio_duration_ms in cases:
        if audio_duration_ms < 0:
            raise ValueError("audio_duration_ms must be non-negative")
        started = time.monotonic()
        result = transcriber.transcribe(request)
        elapsed = time.monotonic() - started
        samples.append(
            ASRBenchmarkSample(
                engine_id=result.engine.engine_id,
                model_id=result.model.model_id,
                chunk_id=request.chunk_id,
                audio_duration_ms=audio_duration_ms,
                elapsed_seconds=elapsed,
                segment_count=len(result.segments),
                text_chars=sum(len(segment.text) for segment in result.segments),
            )
        )
    return tuple(samples)
