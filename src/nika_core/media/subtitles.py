from __future__ import annotations

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

_TAG_RE = re.compile(r"\{[^}]*\}|<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True, slots=True)
class SubtitlePolicy:
    preferred_languages: tuple[str, ...] = ("uk", "en")
    force_transcription: bool = False
    allow_translated: bool = False
    automatic_min_segments: int = 3
    automatic_max_malformed_ratio: float = 0.05
    automatic_min_coverage_ratio: float = 0.55


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
        for exact in (True, False):
            for wanted in preferred:
                wanted_base = wanted.split("-", 1)[0]
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
        if end_ms < start_ms or start_ms < previous_start:
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
        if media_duration_seconds and media_duration_seconds > 0:
            covered = max(segment.end_ms for segment in segments) - min(
                segment.start_ms for segment in segments
            )
            coverage_ratio = covered / (media_duration_seconds * 1000)
            if coverage_ratio < active.automatic_min_coverage_ratio:
                raise MediaError(
                    MediaErrorCode.LOW_QUALITY_SUBTITLE,
                    "automatic subtitle coverage is too low",
                )

    source_sha = sha256_file(path)
    transcript_id = f"subtitle:{source_sha[:32]}"
    return Transcript(
        transcript_id=transcript_id,
        version_id=version_id,
        method=TranscriptMethod.PLATFORM_SUBTITLE,
        language=track.language,
        segments=tuple(segments),
        source_track_id=track.track_id,
    )


def _normalize_language(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _normalize_text(value: str) -> str:
    plain = _TAG_RE.sub("", value).replace("\\N", "\n").replace("\\n", "\n")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in plain.splitlines()]
    return "\n".join(line for line in lines if line).strip()
