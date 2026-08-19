from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.contracts import Segment, SubtitleKind, SubtitleTrack, Transcript, TranscriptMethod
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
    candidates = [track for track in tracks if active.allow_translated or track.kind != SubtitleKind.TRANSLATED]
    if not candidates:
        return None
    preferred = tuple(_normalize_language(item) for item in active.preferred_languages)

    def rank(track: SubtitleTrack) -> tuple[int, int, str, str]:
        language = _normalize_language(track.language)
        base = language.split("-", 1)[0]
        kind_rank = {
            SubtitleKind.MANUAL: 0,
            SubtitleKind.AUTOMATIC: 2,
            SubtitleKind.TRANSLATED: 4,
        }[track.kind]
        language_rank = 100
        for index, wanted in enumerate(preferred):
            wanted_base = wanted.split("-", 1)[0]
            if language == wanted:
                language_rank = index * 10
                break
            if base == wanted_base:
                language_rank = index * 10 + 1
                break
        if language_rank == 100:
            language_rank = 90
        return (kind_rank + language_rank, 0 if track.is_default else 1, language, track.track_id)

    ranked = sorted(candidates, key=rank)
    best = ranked[0]
    best_language = _normalize_language(best.language)
    if not any(
        best_language == wanted or best_language.split("-", 1)[0] == wanted.split("-", 1)[0]
        for wanted in preferred
    ):
        return None
    return best


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
        raise MediaError(MediaErrorCode.INVALID_SUBTITLE, "subtitle file could not be parsed") from exc

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
                segment_id=f"subtitle:{ordinal}:{sha256_json({'s': start_ms, 'e': end_ms, 't': text})[:16]}",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )
    if not segments:
        raise MediaError(MediaErrorCode.LOW_QUALITY_SUBTITLE, "subtitle track contains no usable text")
    total_events = max(1, len(subtitles))
    malformed_ratio = malformed / total_events
    if track.kind == SubtitleKind.AUTOMATIC:
        if len(segments) < active.automatic_min_segments:
            raise MediaError(MediaErrorCode.LOW_QUALITY_SUBTITLE, "automatic subtitle has too few segments")
        if malformed_ratio > active.automatic_max_malformed_ratio:
            raise MediaError(MediaErrorCode.LOW_QUALITY_SUBTITLE, "automatic subtitle has too many malformed segments")
        if media_duration_seconds and media_duration_seconds > 0:
            covered = max(segment.end_ms for segment in segments) - min(segment.start_ms for segment in segments)
            coverage_ratio = covered / (media_duration_seconds * 1000)
            if coverage_ratio < active.automatic_min_coverage_ratio:
                raise MediaError(MediaErrorCode.LOW_QUALITY_SUBTITLE, "automatic subtitle coverage is too low")

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
