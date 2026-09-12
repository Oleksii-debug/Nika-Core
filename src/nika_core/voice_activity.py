from __future__ import annotations

import dataclasses
import math
import struct
from collections.abc import Buffer


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceActivityConfig:
    """Configuration for deterministic PCM16 voice-activity gating.

    The detector intentionally owns no microphone, persistence, wake-word,
    transcription, or speaker-identity authority. It consumes one mono PCM16-LE
    frame at a time and exposes only bounded numeric activity evidence.
    """

    sample_rate_hz: int = 16_000
    start_rms: float = 0.03
    stop_rms: float = 0.02
    attack_frames: int = 2
    release_frames: int = 5
    max_frame_ms: int = 100

    def __post_init__(self) -> None:
        _require_int_range(
            self.sample_rate_hz,
            "sample_rate_hz",
            minimum=8_000,
            maximum=192_000,
        )
        _require_positive_unit(self.start_rms, "start_rms")
        _require_positive_unit(self.stop_rms, "stop_rms")
        if self.stop_rms > self.start_rms:
            raise ValueError("stop_rms must be less than or equal to start_rms")
        _require_int_range(
            self.attack_frames,
            "attack_frames",
            minimum=1,
            maximum=1_000,
        )
        _require_int_range(
            self.release_frames,
            "release_frames",
            minimum=1,
            maximum=1_000,
        )
        _require_int_range(
            self.max_frame_ms,
            "max_frame_ms",
            minimum=1,
            maximum=1_000,
        )

    @property
    def max_frame_bytes(self) -> int:
        max_samples = self.sample_rate_hz * self.max_frame_ms // 1_000
        return max_samples * 2


@dataclasses.dataclass(frozen=True, slots=True)
class VoiceActivityDecision:
    """Activity result for one input frame.

    ``speech_active`` is the debounced stream state after consuming the frame.
    ``started`` and ``ended`` are single-frame transition markers.
    """

    speech_active: bool
    started: bool
    ended: bool
    rms: float
    sample_count: int


class VoiceActivityDetector:
    """Small stateful VAD gate for bounded mono signed PCM16-LE frames.

    This is an acoustic-energy gate, not speaker verification and not a learned
    speech classifier. Hysteresis plus attack/release counters suppress threshold
    chatter while keeping the implementation deterministic and dependency-free.
    """

    def __init__(self, config: VoiceActivityConfig | None = None) -> None:
        resolved = VoiceActivityConfig() if config is None else config
        if type(resolved) is not VoiceActivityConfig:
            raise TypeError("config must be VoiceActivityConfig")
        self._config = resolved
        self.reset()

    @property
    def config(self) -> VoiceActivityConfig:
        return self._config

    @property
    def speech_active(self) -> bool:
        return self._speech_active

    def reset(self) -> None:
        self._speech_active = False
        self._attack_run = 0
        self._release_run = 0

    def process(self, pcm16le: Buffer) -> VoiceActivityDecision:
        raw = _bounded_pcm16_bytes(pcm16le, maximum=self._config.max_frame_bytes)
        rms, sample_count = _normalized_rms(raw)

        started = False
        ended = False
        if self._speech_active:
            if rms < self._config.stop_rms:
                self._release_run += 1
            else:
                self._release_run = 0
            if self._release_run >= self._config.release_frames:
                self._speech_active = False
                self._attack_run = 0
                self._release_run = 0
                ended = True
        else:
            if rms >= self._config.start_rms:
                self._attack_run += 1
            else:
                self._attack_run = 0
            if self._attack_run >= self._config.attack_frames:
                self._speech_active = True
                self._attack_run = 0
                self._release_run = 0
                started = True

        return VoiceActivityDecision(
            speech_active=self._speech_active,
            started=started,
            ended=ended,
            rms=rms,
            sample_count=sample_count,
        )


def _bounded_pcm16_bytes(value: Buffer, *, maximum: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("pcm16le must be bytes-like")
    try:
        view = memoryview(value)
        byte_count = view.nbytes
    except (TypeError, ValueError) as exc:
        raise ValueError("pcm16le buffer is unavailable") from exc
    if byte_count == 0:
        raise ValueError("pcm16le must not be empty")
    if byte_count % 2:
        raise ValueError("pcm16le must contain complete 16-bit samples")
    if byte_count > maximum:
        raise ValueError("pcm16le exceeds configured frame bound")
    return view.tobytes()


def _normalized_rms(raw: bytes) -> tuple[float, int]:
    sample_count = len(raw) // 2
    square_sum = 0
    for (sample,) in struct.iter_unpack("<h", raw):
        square_sum += sample * sample
    rms = math.sqrt(square_sum / sample_count) / 32_768.0
    return rms, sample_count


def _require_int_range(value: int, name: str, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _require_positive_unit(value: float, name: str) -> None:
    if type(value) not in {int, float}:
        raise ValueError(f"{name} must be a finite number greater than 0 and at most 1")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 < numeric <= 1:
        raise ValueError(f"{name} must be a finite number greater than 0 and at most 1")
