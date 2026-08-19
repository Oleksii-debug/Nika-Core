from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.media.contracts import EngineDescriptor, Probe
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_bytes, sha256_file
from nika_core.media.process import SafeProcessRunner

_VERSION_RE = re.compile(r"ffprobe version\s+([^\s]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FFprobeAudit:
    descriptor: EngineDescriptor
    license_classification: str


class FFprobeAdapter:
    def __init__(self, executable: Path, runner: SafeProcessRunner | None = None) -> None:
        if not executable.is_file():
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                "ffprobe executable is missing; Nika will not download it automatically",
            )
        self._executable = executable.resolve(strict=True)
        self._runner = runner or SafeProcessRunner(max_output_bytes=8 * 1024 * 1024)

    def audit(self, *, cwd: Path, timeout_seconds: float = 15.0) -> FFprobeAudit:
        version_result = self._runner.run(
            (str(self._executable), "-version"),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        build_result = self._runner.run(
            (str(self._executable), "-buildconf"),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        version_text = version_result.stdout.decode("utf-8", errors="replace")
        match = _VERSION_RE.search(version_text)
        if match is None:
            raise MediaError(MediaErrorCode.PROBE_FAILED, "unable to identify ffprobe version")
        buildconf = build_result.stdout.decode("utf-8", errors="replace")
        classification = _classify_ffmpeg_license(buildconf)
        descriptor = EngineDescriptor(
            engine_id="ffprobe",
            name="FFmpeg ffprobe",
            version=match.group(1),
            license_id=classification,
            source_reference="https://ffmpeg.org/",
            executable_sha256=sha256_file(self._executable),
            build_configuration=buildconf[:12000],
        )
        return FFprobeAudit(descriptor=descriptor, license_classification=classification)

    def probe(
        self,
        media_path: Path,
        *,
        asset_id: str,
        cwd: Path,
        timeout_seconds: float = 30.0,
    ) -> Probe:
        resolved = media_path.resolve(strict=True)
        result = self._runner.run(
            (
                str(self._executable),
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(resolved),
            ),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaError(MediaErrorCode.PROBE_FAILED, "ffprobe returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MediaError(MediaErrorCode.PROBE_FAILED, "ffprobe response must be an object")
        format_info = payload.get("format") or {}
        streams = payload.get("streams") or []
        duration = _optional_nonnegative_float(format_info.get("duration"), "duration")
        bit_rate = _optional_nonnegative_int(format_info.get("bit_rate"), "bit_rate")
        format_name = format_info.get("format_name")
        normalized_streams = tuple(_normalize_stream(item) for item in streams if isinstance(item, dict))
        return Probe(
            asset_id=asset_id,
            container=str(format_name) if format_name is not None else None,
            duration_seconds=duration,
            bit_rate=bit_rate,
            streams=normalized_streams,
            raw_sha256=sha256_bytes(result.stdout),
        )


def _classify_ffmpeg_license(buildconf: str) -> str:
    normalized = buildconf.lower()
    if "--enable-nonfree" in normalized:
        return "FFmpeg-NONFREE-BUILD-REVIEW-REQUIRED"
    if "--enable-gpl" in normalized or "--enable-version3" in normalized:
        return "GPL-2.0-or-later/build-dependent"
    return "LGPL-2.1-or-later/build-dependent"


def _normalize_stream(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "index",
        "codec_name",
        "codec_type",
        "sample_rate",
        "channels",
        "channel_layout",
        "width",
        "height",
        "duration",
        "bit_rate",
    )
    result = {key: item.get(key) for key in allowed if key in item}
    tags = item.get("tags")
    if isinstance(tags, dict):
        safe_tags = {}
        for key in ("language", "title"):
            if key in tags:
                safe_tags[key] = str(tags[key])[:300]
        if safe_tags:
            result["tags"] = safe_tags
    return result


def _optional_nonnegative_float(value: Any, name: str) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(MediaErrorCode.PROBE_FAILED, f"invalid ffprobe {name}") from exc
    if number < 0:
        raise MediaError(MediaErrorCode.PROBE_FAILED, f"ffprobe {name} must be nonnegative")
    return number


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(MediaErrorCode.PROBE_FAILED, f"invalid ffprobe {name}") from exc
    if number < 0:
        raise MediaError(MediaErrorCode.PROBE_FAILED, f"ffprobe {name} must be nonnegative")
    return number
