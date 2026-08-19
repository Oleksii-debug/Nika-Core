from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.media.contracts import (
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    SubtitleKind,
    SubtitleTrack,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_json
from nika_core.media.privacy import redact_mapping
from nika_core.media.process import SafeProcessRunner


@dataclass(frozen=True, slots=True)
class YtDlpPolicy:
    max_duration_seconds: float = 6 * 60 * 60
    allow_playlists: bool = False
    max_playlist_entries: int = 1
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class YtDlpDiscovery:
    source: MediaSource
    version: MediaVersion
    subtitles: tuple[SubtitleTrack, ...]
    formats: tuple[dict[str, Any], ...]
    sanitized_metadata: dict[str, Any]


class YtDlpAdapter:
    def __init__(self, runner: SafeProcessRunner | None = None) -> None:
        self._runner = runner or SafeProcessRunner(max_output_bytes=16 * 1024 * 1024)

    def discover(
        self,
        url: str,
        *,
        cwd: Path,
        policy: YtDlpPolicy | None = None,
        privacy: str = "private",
        auth_ref: str | None = None,
    ) -> YtDlpDiscovery:
        active_policy = policy or YtDlpPolicy()
        self._validate_url(url)
        if auth_ref is not None:
            raise MediaError(
                MediaErrorCode.AUTH_REQUIRED,
                "Authenticated media requires an explicit credential-resolution product action; "
                "browser cookies are never loaded automatically.",
            )
        argv = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
        ]
        if not active_policy.allow_playlists:
            argv.append("--no-playlist")
        argv.append(url)
        try:
            result = self._runner.run(
                argv,
                cwd=cwd,
                timeout_seconds=active_policy.timeout_seconds,
            )
        except MediaError as exc:
            raise self._normalize_process_error(exc) from exc
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaError(MediaErrorCode.INVALID_METADATA, "yt-dlp returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MediaError(MediaErrorCode.INVALID_METADATA, "yt-dlp metadata must be an object")
        return self._normalize(payload, url=url, privacy=privacy, policy=active_policy)

    def _normalize(
        self,
        payload: dict[str, Any],
        *,
        url: str,
        privacy: str,
        policy: YtDlpPolicy,
    ) -> YtDlpDiscovery:
        if payload.get("_type") == "playlist" or isinstance(payload.get("entries"), list):
            entries = payload.get("entries") or []
            if not policy.allow_playlists:
                raise MediaError(MediaErrorCode.PLAYLIST_LIMIT, "playlists are disabled by media policy")
            if len(entries) > policy.max_playlist_entries:
                raise MediaError(
                    MediaErrorCode.PLAYLIST_LIMIT,
                    "playlist exceeds the configured entry limit",
                )
            raise MediaError(
                MediaErrorCode.UNSUPPORTED_SOURCE,
                "playlist discovery is bounded but Batch A accepts a single media item per job",
            )

        duration = self._optional_float(payload.get("duration"))
        if duration is not None and duration > policy.max_duration_seconds:
            raise MediaError(
                MediaErrorCode.DURATION_LIMIT,
                "media duration exceeds the configured processing limit",
            )
        upstream_id = str(payload.get("id") or "").strip() or None
        canonical_url = str(payload.get("webpage_url") or payload.get("original_url") or url)
        source_id = f"remote:{sha256_json({'url': canonical_url})[:32]}"
        source = MediaSource(
            source_id=source_id,
            kind=MediaSourceKind.REMOTE_MEDIA,
            locator=canonical_url,
            privacy=privacy,
        )
        safe_metadata = self._safe_metadata(payload)
        metadata_sha = sha256_json(safe_metadata)
        version_basis = upstream_id or metadata_sha
        version = MediaVersion(
            version_id=(
                "remote-version:"
                f"{sha256_json({'source': source_id, 'v': version_basis})[:32]}"
            ),
            source_id=source_id,
            metadata_sha256=metadata_sha,
            title=str(payload.get("title") or "")[:1000],
            duration_seconds=duration,
            upstream_id=upstream_id,
        )
        subtitles = self._subtitle_tracks(payload)
        formats = self._formats(payload)
        return YtDlpDiscovery(
            source=source,
            version=version,
            subtitles=subtitles,
            formats=formats,
            sanitized_metadata=safe_metadata,
        )

    @staticmethod
    def _safe_metadata(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id",
            "title",
            "description",
            "duration",
            "timestamp",
            "upload_date",
            "uploader",
            "uploader_id",
            "channel",
            "channel_id",
            "webpage_url",
            "original_url",
            "extractor",
            "extractor_key",
            "live_status",
            "availability",
            "age_limit",
        }
        selected = {key: payload.get(key) for key in allowed if key in payload}
        return redact_mapping(selected)

    @staticmethod
    def _formats(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        normalized: list[dict[str, Any]] = []
        for item in payload.get("formats") or []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "format_id": str(item.get("format_id") or ""),
                    "ext": str(item.get("ext") or ""),
                    "acodec": str(item.get("acodec") or ""),
                    "vcodec": str(item.get("vcodec") or ""),
                    "language": item.get("language"),
                    "filesize": item.get("filesize") or item.get("filesize_approx"),
                }
            )
        return tuple(normalized)

    @staticmethod
    def _subtitle_tracks(payload: dict[str, Any]) -> tuple[SubtitleTrack, ...]:
        tracks: list[SubtitleTrack] = []
        for field, kind in (
            ("subtitles", SubtitleKind.MANUAL),
            ("automatic_captions", SubtitleKind.AUTOMATIC),
        ):
            catalog = payload.get(field) or {}
            if not isinstance(catalog, dict):
                continue
            for language, candidates in sorted(catalog.items()):
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    ext = str(candidate.get("ext") or "vtt")
                    url = candidate.get("url")
                    if url is not None:
                        url = str(url)
                    tracks.append(
                        SubtitleTrack(
                            track_id=str(uuid.uuid4()),
                            language=str(language),
                            kind=kind,
                            name=str(candidate.get("name") or ""),
                            url=url,
                            format=ext,
                            source_label=field,
                        )
                    )
        return tuple(tracks)

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MediaError(MediaErrorCode.INVALID_METADATA, "invalid media duration") from exc
        if number < 0:
            raise MediaError(MediaErrorCode.INVALID_METADATA, "media duration must not be negative")
        return number

    @staticmethod
    def _validate_url(url: str) -> None:
        lowered = url.strip().lower()
        if not lowered.startswith(("https://", "http://")):
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL must use HTTP(S)")
        if any(char in url for char in ("\x00", "\r", "\n")):
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL contains invalid characters")

    @staticmethod
    def _normalize_process_error(error: MediaError) -> MediaError:
        message = str(error).lower()
        if "unsupported url" in message:
            return MediaError(MediaErrorCode.UNSUPPORTED_SOURCE, "media source is unsupported")
        if "sign in" in message or "login" in message or "cookies" in message:
            return MediaError(
                MediaErrorCode.AUTH_REQUIRED,
                "media source requires explicit authentication",
            )
        if error.code == MediaErrorCode.PROCESS_TIMEOUT:
            return error
        return MediaError(MediaErrorCode.PROCESS_FAILED, "yt-dlp metadata discovery failed")
