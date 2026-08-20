from __future__ import annotations

import ipaddress
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nika_core.media.contracts import (
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    SubtitleKind,
    SubtitleTrack,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_json
from nika_core.media.privacy import redact_mapping, redact_text
from nika_core.media.process import SafeProcessRunner

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "key",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


@dataclass(frozen=True, slots=True)
class YtDlpPolicy:
    max_duration_seconds: float = 6 * 60 * 60
    allow_playlists: bool = False
    max_playlist_entries: int = 1
    timeout_seconds: float = 60.0
    max_url_length: int = 8192
    max_formats: int = 512
    max_subtitle_tracks: int = 512
    allow_private_networks: bool = False

    def __post_init__(self) -> None:
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if self.max_playlist_entries < 1:
            raise ValueError("max_playlist_entries must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_url_length < 1 or self.max_url_length > 65536:
            raise ValueError("max_url_length must be between 1 and 65536")
        if self.max_formats < 1 or self.max_formats > 10000:
            raise ValueError("max_formats must be between 1 and 10000")
        if self.max_subtitle_tracks < 1 or self.max_subtitle_tracks > 10000:
            raise ValueError("max_subtitle_tracks must be between 1 and 10000")


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
        self._validate_source_url(url, policy=active_policy)
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
        raw_canonical_url = str(payload.get("webpage_url") or payload.get("original_url") or url)
        canonical_url = self._sanitize_url_for_persistence(raw_canonical_url)
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
        subtitles = self._subtitle_tracks(payload, policy=policy)
        formats = self._formats(payload, policy=policy)
        return YtDlpDiscovery(
            source=source,
            version=version,
            subtitles=subtitles,
            formats=formats,
            sanitized_metadata=safe_metadata,
        )

    @classmethod
    def _safe_metadata(cls, payload: dict[str, Any]) -> dict[str, Any]:
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
        for key in ("webpage_url", "original_url"):
            if isinstance(selected.get(key), str):
                selected[key] = cls._sanitize_url_for_persistence(selected[key])
        return redact_mapping(selected)

    @staticmethod
    def _formats(payload: dict[str, Any], *, policy: YtDlpPolicy) -> tuple[dict[str, Any], ...]:
        raw_formats = payload.get("formats") or []
        if not isinstance(raw_formats, list):
            raise MediaError(MediaErrorCode.INVALID_METADATA, "yt-dlp formats must be a list")
        if len(raw_formats) > policy.max_formats:
            raise MediaError(MediaErrorCode.METADATA_LIMIT, "format catalog exceeds configured limit")
        normalized: list[dict[str, Any]] = []
        for item in raw_formats:
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

    @classmethod
    def _subtitle_tracks(
        cls,
        payload: dict[str, Any],
        *,
        policy: YtDlpPolicy,
    ) -> tuple[SubtitleTrack, ...]:
        tracks: list[SubtitleTrack] = []
        for field, kind in (
            ("subtitles", SubtitleKind.MANUAL),
            ("automatic_captions", SubtitleKind.AUTOMATIC),
        ):
            catalog = payload.get(field) or {}
            if not isinstance(catalog, dict):
                raise MediaError(MediaErrorCode.INVALID_METADATA, f"yt-dlp {field} must be an object")
            for language, candidates in sorted(catalog.items()):
                if not isinstance(candidates, list):
                    continue
                for candidate_index, candidate in enumerate(candidates):
                    if not isinstance(candidate, dict):
                        continue
                    if len(tracks) >= policy.max_subtitle_tracks:
                        raise MediaError(
                            MediaErrorCode.METADATA_LIMIT,
                            "subtitle catalog exceeds configured limit",
                        )
                    ext = str(candidate.get("ext") or "vtt")
                    raw_url = candidate.get("url")
                    safe_url = (
                        cls._sanitize_url_for_persistence(str(raw_url))
                        if raw_url is not None
                        else None
                    )
                    track_basis = {
                        "field": field,
                        "kind": kind.value,
                        "language": str(language),
                        "ext": ext,
                        "name": str(candidate.get("name") or ""),
                        "index": candidate_index,
                    }
                    tracks.append(
                        SubtitleTrack(
                            track_id=f"subtitle-track:{sha256_json(track_basis)[:32]}",
                            language=str(language),
                            kind=kind,
                            name=str(candidate.get("name") or ""),
                            url=safe_url,
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

    @classmethod
    def _validate_source_url(cls, url: str, *, policy: YtDlpPolicy) -> None:
        if not isinstance(url, str):
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL must be text")
        if len(url) > policy.max_url_length:
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL exceeds configured limit")
        if any(char in url for char in ("\x00", "\r", "\n")):
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL contains invalid characters")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL is malformed") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise MediaError(MediaErrorCode.INVALID_SOURCE, "remote media URL must use HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise MediaError(
                MediaErrorCode.AUTH_REQUIRED,
                "URL-embedded credentials are forbidden; use an opaque authentication reference",
            )
        query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
        if query_keys & _SENSITIVE_QUERY_KEYS:
            raise MediaError(
                MediaErrorCode.AUTH_REQUIRED,
                "credential-like URL query parameters are forbidden; use an opaque authentication reference",
            )
        if not policy.allow_private_networks and cls._is_private_network_host(hostname):
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                "private, loopback, link-local, or reserved network targets are disabled by media policy",
            )

    @staticmethod
    def _is_private_network_host(hostname: str) -> bool:
        normalized = hostname.rstrip(".").lower()
        if normalized in _LOCAL_HOSTNAMES or normalized.endswith(".localhost"):
            return True
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        return not address.is_global

    @staticmethod
    def _sanitize_url_for_persistence(url: str) -> str:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError:
            return redact_text(url)
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return redact_text(url)
        host = hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        safe_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            safe_query.append((key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value))
        sanitized = urlunsplit(
            (
                parsed.scheme.lower(),
                host,
                parsed.path,
                urlencode(safe_query, doseq=True),
                "",
            )
        )
        return redact_text(sanitized)

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
