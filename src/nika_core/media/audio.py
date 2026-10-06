from __future__ import annotations

import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.files import PromotedFile, promote_partial_file
from nika_core.media.process import SafeProcessRunner


@dataclass(frozen=True, slots=True)
class AudioExtractionPolicy:
    sample_rate_hz: int = 16_000
    channels: int = 1
    codec: str = "pcm_s16le"
    timeout_seconds: float = 900.0
    max_output_bytes: int = 4 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.channels not in {1, 2}:
            raise ValueError("channels must be 1 or 2")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("audio extraction bounds must be positive")


@dataclass(frozen=True, slots=True)
class WavInspection:
    sample_rate_hz: int
    channels: int
    frames: int
    duration_ms: int
    peak_pcm16: int

    @property
    def is_empty(self) -> bool:
        return self.frames == 0

    def is_silent(self, *, peak_threshold: int = 8) -> bool:
        if peak_threshold < 0:
            raise ValueError("peak_threshold must be non-negative")
        return self.frames == 0 or self.peak_pcm16 <= peak_threshold


def inspect_pcm16_wav(path: Path, *, max_frames: int = 16000 * 60 * 10) -> WavInspection:
    if max_frames <= 0:
        raise ValueError("max_frames must be positive")
    resolved = path.resolve(strict=True)
    with wave.open(str(resolved), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        if channels not in {1, 2} or sample_width != 2 or sample_rate <= 0:
            raise ValueError("expected mono/stereo PCM16 WAV with a positive sample rate")
        if frames > max_frames:
            raise ValueError("WAV inspection frame limit exceeded")
        raw = handle.readframes(frames)
    peak = 0
    for offset in range(0, len(raw) - 1, 2):
        sample = int.from_bytes(raw[offset : offset + 2], "little", signed=True)
        peak = max(peak, abs(sample))
    return WavInspection(
        sample_rate_hz=sample_rate,
        channels=channels,
        frames=frames,
        duration_ms=round(frames / sample_rate * 1000),
        peak_pcm16=peak,
    )


class FFmpegAudioExtractor:
    """Extract deterministic PCM WAV audio through the shared safe subprocess boundary."""

    def __init__(self, *, ffmpeg_path: Path, runner: SafeProcessRunner | None = None) -> None:
        executable = ffmpeg_path.resolve(strict=True)
        if not executable.is_file():
            raise ValueError("ffmpeg_path must be a file")
        self._ffmpeg_path = executable
        self._runner = runner or SafeProcessRunner(max_output_bytes=4 * 1024 * 1024)

    def extract(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        allowed_root: Path,
        policy: AudioExtractionPolicy | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> PromotedFile:
        selected = policy or AudioExtractionPolicy()
        root = allowed_root.resolve(strict=True)
        source = source_path.resolve(strict=True)
        destination_parent = destination_path.parent.resolve(strict=True)
        try:
            source.relative_to(root)
            destination_parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("audio input/output must stay inside allowed_root") from exc
        if destination_path.suffix.lower() != ".wav":
            raise ValueError("normalized audio destination must use .wav")
        if (start_ms is None) != (end_ms is None):
            raise ValueError("start_ms and end_ms must be supplied together")
        if (
            start_ms is not None
            and end_ms is not None
            and (start_ms < 0 or end_ms <= start_ms)
        ):
            raise ValueError("clip bounds must satisfy 0 <= start_ms < end_ms")
        partial = destination_path.with_suffix(destination_path.suffix + ".partial")
        if partial.exists():
            partial.unlink()
        argv: list[str] = [
            str(self._ffmpeg_path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        if start_ms is not None and end_ms is not None:
            argv.extend(
                ("-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}")
            )
        argv.extend(
            (
                "-i",
                str(source),
                "-vn",
                "-map_metadata",
                "-1",
                "-ac",
                str(selected.channels),
                "-ar",
                str(selected.sample_rate_hz),
                "-c:a",
                selected.codec,
                "-f",
                "wav",
                str(partial),
            )
        )
        try:
            self._runner.run(
                argv,
                cwd=root,
                timeout_seconds=selected.timeout_seconds,
                cancel_event=cancel_event,
            )
            return promote_partial_file(
                partial,
                destination_path,
                allowed_root=root,
                max_bytes=selected.max_output_bytes,
            )
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
