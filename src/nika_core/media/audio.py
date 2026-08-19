from __future__ import annotations

import threading
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
        partial = destination_path.with_suffix(destination_path.suffix + ".partial")
        if partial.exists():
            partial.unlink()
        argv = (
            str(self._ffmpeg_path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
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
