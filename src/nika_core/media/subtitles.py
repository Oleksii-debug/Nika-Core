from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.contracts import (
    Segment,
    SubtitleKind,
    SubtitleTrack,
    Transcript,
    TranscriptMethod,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file, sha256_json

_TAG_RE = re.compile(r"\{\\[^}]*\}|</?[A-Za-z][^>]*>|<\d{2}:\d{2}(?::\d{2})?\.\d{3}>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True, slots=True)
class SubtitlePolicy:
    preferred_languages: tuple[str, ...] = ("uk", "en")
    force_transcription: bool = False
    allow_translated: bool = False
    automatic_min_segments: int = 3
    automatic_max_malformed_ratio: float = 0.05
    automatic_min_coverage_ratio: float = 0.55

    def __post_init__(self) -> None:
        if type(self.automatic_min_segments) is not int or self.automatic_min_segments < 1:
            raise ValueError("automatic_min_segments must be a positive integer")
        for name, value in (
            ("automatic_max_malformed_ratio", self.automatic_max_malformed_ratio),
            ("automatic_min_coverage_ratio", self.automatic_min_coverage_ratio),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be a finite ratio in [0, 1]")


def select_subtitle_track(
    tracks: tuple[SubtitleTrack, ...] | list[SubtitleTrack],
    *,
    policy: SubtitlePolicy | None = None,
) -> SubtitleTrack | None:
    active = policy or SubtitlePolicy()
    if active.force_transcription:
        return None
    preferred = tuple(_normalize_language(item) for item in active.preferred_languages)
    if not preferred:
        return None
    ordered = sorted(tracks, key=lambda item: (not item.is_default, item.track_id))
    kinds = [SubtitleKind.MANUAL, SubtitleKind.AUTOMATIC]
    if active.allow_translated:
        kinds.append(SubtitleKind.TRANSLATED)

    for kind in kinds:
        same_kind = [track for track in ordered if track.kind == kind]
        for wanted in preferred:
            wanted_base = wanted.split("-", 1)[0]
            for exact in (True, False):
                matches = []
                for track in same_kind:
                    language = _normalize_language(track.language)
                    is_match = (exact and language == wanted) or (
                        not exact
                        and language != wanted
                        and language.split("-", 1)[0] == wanted_base
                    )
                    if is_match:
                        matches.append(track)
                if matches:
                    return matches[0]
    return None


def normalize_subtitle_file(
    path: Path,
    *,
    track: SubtitleTrack,
    version_id: str,
    media_duration_seconds: float | None,
    policy: SubtitlePolicy | None = None,
) -> Transcript:
    active = policy or SubtitlePolicy()
    try:
        import pysubs2
    except ImportError as exc:
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            "pysubs2 is not installed; install the optional media component explicitly",
        ) from exc
    try:
        source_sha = sha256_file(path)
    except OSError as exc:
        raise MediaError(
            MediaErrorCode.INVALID_SUBTITLE,
            "subtitle source could not be read",
        ) from exc
    try:
        subtitles = pysubs2.load(str(path), encoding="utf-8")
    except Exception as exc:
        raise MediaError(
            MediaErrorCode.INVALID_SUBTITLE,
            "subtitle file could not be parsed",
        ) from exc

    segments: list[Segment] = []
    malformed = 0
    previous_start = -1
    for ordinal, event in enumerate(subtitles):
        start_ms = int(event.start)
        end_ms = int(event.end)
        text = _normalize_text(str(event.text))
        if start_ms < 0 or end_ms < start_ms or start_ms < previous_start:
            malformed += 1
            continue
        previous_start = start_ms
        if not text:
            continue
        segments.append(
            Segment(
                segment_id=(
                    "subtitle:"
                    f"{ordinal}:{sha256_json({'s': start_ms, 'e': end_ms, 't': text})[:16]}"
                ),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )
    if not segments:
        raise MediaError(
            MediaErrorCode.LOW_QUALITY_SUBTITLE,
            "subtitle track contains no usable text",
        )
    total_events = max(1, len(subtitles))
    malformed_ratio = malformed / total_events
    if track.kind == SubtitleKind.AUTOMATIC:
        if len(segments) < active.automatic_min_segments:
            raise MediaError(
                MediaErrorCode.LOW_QUALITY_SUBTITLE,
                "automatic subtitle has too few segments",
            )
        if malformed_ratio > active.automatic_max_malformed_ratio:
            raise MediaError(
                MediaErrorCode.LOW_QUALITY_SUBTITLE,
                "automatic subtitle has too many malformed segments",
            )
        if media_duration_seconds is not None:
            if not _finite_nonnegative_duration(media_duration_seconds):
                raise MediaError(
                    MediaErrorCode.INVALID_SUBTITLE,
                    "media duration must be a finite nonnegative number",
                )
            if media_duration_seconds > 0:
                coverage_ratio = _covered_duration_ms(
                    segments, duration_ms=media_duration_seconds * 1000
                ) / (media_duration_seconds * 1000)
                if coverage_ratio < active.automatic_min_coverage_ratio:
                    raise MediaError(
                        MediaErrorCode.LOW_QUALITY_SUBTITLE,
                        "automatic subtitle coverage is too low",
                    )

    try:
        unchanged = sha256_file(path) == source_sha
    except OSError as exc:
        raise MediaError(
            MediaErrorCode.INVALID_SUBTITLE,
            "subtitle source could not be revalidated",
        ) from exc
    if not unchanged:
        raise MediaError(
            MediaErrorCode.INVALID_SUBTITLE,
            "subtitle source changed while it was being parsed",
        )
    transcript_id = f"subtitle:{source_sha[:32]}"
    return Transcript(
        transcript_id=transcript_id,
        version_id=version_id,
        method=TranscriptMethod.PLATFORM_SUBTITLE,
        language=track.language,
        segments=tuple(segments),
        source_track_id=track.track_id,
    )


def _finite_nonnegative_duration(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _covered_duration_ms(segments: list[Segment], *, duration_ms: float) -> float:
    """Count the union of caption intervals inside the actual media duration.

    Span between the first and last caption is not evidence that intervening
    dialogue has been transcribed. Overlapping cues must not count twice.
    """
    covered = 0.0
    previous_end = 0.0
    for segment in segments:
        start = min(segment.start_ms, duration_ms)
        end = min(segment.end_ms, duration_ms)
        if end > previous_end:
            covered += end - max(start, previous_end)
            previous_end = end
    return covered


def _normalize_language(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _normalize_text(value: str) -> str:
    plain = _TAG_RE.sub("", value).replace("\\N", "\n").replace("\\n", "\n")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in plain.splitlines()]
    return "\n".join(line for line in lines if line).strip()
