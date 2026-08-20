from __future__ import annotations

import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.contracts import AssetKind, MediaAsset, SubtitleKind, SubtitleTrack, Transcript
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.files import promote_partial_file
from nika_core.media.hashing import sha256_json
from nika_core.media.process import SafeProcessRunner
from nika_core.media.subtitles import SubtitlePolicy, normalize_subtitle_file
from nika_core.media.yt_dlp import YtDlpAdapter, YtDlpDiscovery, YtDlpPolicy

_SUBTITLE_TOKEN_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"
)


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPolicy:
    discovery_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 120.0
    max_bytes: int = 16 * 1024 * 1024
    allow_private_networks: bool = False

    def __post_init__(self) -> None:
        if self.discovery_timeout_seconds <= 0 or self.discovery_timeout_seconds > 600:
            raise ValueError("discovery_timeout_seconds must be between 0 and 600")
        if self.download_timeout_seconds <= 0 or self.download_timeout_seconds > 3600:
            raise ValueError("download_timeout_seconds must be between 0 and 3600")
        if self.max_bytes < 1 or self.max_bytes > 64 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 64 MiB")


@dataclass(frozen=True, slots=True)
class RemoteSubtitleResult:
    asset: MediaAsset
    transcript: Transcript
    rediscovered_track: SubtitleTrack


def stable_subtitle_tracks(discovery: YtDlpDiscovery) -> tuple[SubtitleTrack, ...]:
    """Return persistence-safe track identities without ephemeral upstream locators."""

    return tuple(track.model_copy(update={"url": None}) for track in discovery.subtitles)


class YtDlpSubtitleAcquirer:
    """Materialize one platform subtitle by stable track identity.

    Subtitle CDN URLs discovered by yt-dlp are treated as ephemeral implementation details.
    Before every materialization attempt Nika re-runs metadata discovery from the stable source
    page URL and requires the same ``track_id``. yt-dlp then resolves the current locator inside
    its own fixed subprocess invocation. Only checksum-bound bytes and normalized Nika contracts
    leave this adapter.
    """

    def __init__(
        self,
        runner: SafeProcessRunner | None = None,
        discovery: YtDlpAdapter | None = None,
    ) -> None:
        self._runner = runner or SafeProcessRunner(max_output_bytes=4 * 1024 * 1024)
        self._discovery = discovery or YtDlpAdapter()

    def acquire_subtitle(
        self,
        source_url: str,
        *,
        version_id: str,
        track_id: str,
        output_root: Path,
        media_duration_seconds: float | None,
        subtitle_policy: SubtitlePolicy | None = None,
        policy: SubtitleAcquisitionPolicy | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RemoteSubtitleResult:
        active = policy or SubtitleAcquisitionPolicy()
        root = output_root.resolve(strict=True)
        fresh = self._discovery.discover(
            source_url,
            cwd=root,
            policy=YtDlpPolicy(
                timeout_seconds=active.discovery_timeout_seconds,
                allow_private_networks=active.allow_private_networks,
            ),
        )
        if fresh.version.version_id != version_id:
            raise MediaError(
                MediaErrorCode.INVALID_METADATA,
                "remote media version changed during subtitle rediscovery",
            )
        refreshed = next(
            (candidate for candidate in fresh.subtitles if candidate.track_id == track_id),
            None,
        )
        if refreshed is None:
            raise MediaError(
                MediaErrorCode.INVALID_SUBTITLE,
                "selected subtitle track is no longer available after rediscovery",
            )
        if refreshed.kind not in {SubtitleKind.MANUAL, SubtitleKind.AUTOMATIC}:
            raise MediaError(
                MediaErrorCode.UNSUPPORTED_SOURCE,
                "translated subtitle materialization is not supported by this acquisition path",
            )
        self._validate_token(refreshed.language, field="subtitle language")
        self._validate_token(refreshed.format, field="subtitle format")
        persistence_safe_track = refreshed.model_copy(update={"url": None})

        stable_stem = (
            "remote-"
            f"{sha256_json({'version_id': version_id, 'track_id': track_id})[:32]}"
        )
        final_path = root / f"{stable_stem}.subtitle.{refreshed.format}"
        partial_path = root / f"{stable_stem}.subtitle.{refreshed.format}.partial"
        staging = root / f".{stable_stem}.subtitle-staging"
        if final_path.exists():
            raise FileExistsError(f"refusing to overwrite existing subtitle asset: {final_path.name}")
        if partial_path.exists():
            raise MediaError(
                MediaErrorCode.PROCESS_FAILED,
                "completed subtitle partial requires explicit reconciliation before retry",
            )
        staging.mkdir(exist_ok=True)
        self._validate_staging(staging, max_bytes=active.max_bytes)

        argv = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--continue",
            "--no-write-info-json",
            "--no-write-comments",
            "--no-write-thumbnail",
            "--sub-langs",
            refreshed.language,
            "--sub-format",
            refreshed.format,
            "--max-filesize",
            str(active.max_bytes),
            "--write-subs" if refreshed.kind == SubtitleKind.MANUAL else "--write-auto-subs",
            "-o",
            str(staging / "track.%(ext)s"),
            source_url,
        ]
        try:
            self._runner.run(
                argv,
                cwd=staging,
                timeout_seconds=active.download_timeout_seconds,
                cancel_event=cancel_event,
            )
        except MediaError:
            self._validate_staging(staging, max_bytes=active.max_bytes)
            raise

        candidates = self._completed_files(staging)
        if len(candidates) != 1:
            raise MediaError(
                MediaErrorCode.PROCESS_FAILED,
                "yt-dlp must produce exactly one completed subtitle file",
            )
        candidate = candidates[0]
        if candidate.stat().st_size == 0:
            raise MediaError(MediaErrorCode.INVALID_SUBTITLE, "downloaded subtitle file is empty")
        if candidate.stat().st_size > active.max_bytes:
            candidate.unlink(missing_ok=True)
            raise MediaError(
                MediaErrorCode.SOURCE_TOO_LARGE,
                "subtitle output exceeds configured byte limit",
            )

        os.replace(candidate, partial_path)
        promoted = promote_partial_file(
            partial_path,
            final_path,
            allowed_root=root,
            max_bytes=active.max_bytes,
        )
        transcript = normalize_subtitle_file(
            promoted.path,
            track=persistence_safe_track,
            version_id=version_id,
            media_duration_seconds=media_duration_seconds,
            policy=subtitle_policy,
        )
        shutil.rmtree(staging, ignore_errors=True)
        asset = MediaAsset(
            asset_id=f"subtitle-asset:{promoted.sha256[:32]}",
            version_id=version_id,
            kind=AssetKind.SUBTITLE,
            relative_path=promoted.path.relative_to(root).as_posix(),
            sha256=promoted.sha256,
            size_bytes=promoted.size_bytes,
            media_type=self._media_type(refreshed.format),
            immutable_original=True,
        )
        return RemoteSubtitleResult(
            asset=asset,
            transcript=transcript,
            rediscovered_track=persistence_safe_track,
        )

    @staticmethod
    def _validate_token(value: str, *, field: str) -> None:
        if (
            not value
            or len(value) > 80
            or any(character not in _SUBTITLE_TOKEN_ALLOWED for character in value)
            or any(character.isspace() for character in value)
        ):
            raise ValueError(f"{field} contains unsupported characters")

    @staticmethod
    def _completed_files(staging: Path) -> list[Path]:
        return sorted(
            path
            for path in staging.iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        )

    @staticmethod
    def _validate_staging(staging: Path, *, max_bytes: int) -> None:
        total_bytes = 0
        for path in staging.iterdir():
            if not path.is_file():
                raise MediaError(
                    MediaErrorCode.PATH_ESCAPE,
                    "subtitle staging directory contains an unexpected non-file entry",
                )
            size = path.stat().st_size
            total_bytes += size
            if size > max_bytes or total_bytes > max_bytes:
                path.unlink(missing_ok=True)
                raise MediaError(
                    MediaErrorCode.SOURCE_TOO_LARGE,
                    "subtitle staging output exceeds configured byte limit",
                )

    @staticmethod
    def _media_type(format_name: str) -> str:
        return {
            "vtt": "text/vtt",
            "srt": "application/x-subrip",
            "ass": "text/x-ssa",
            "ssa": "text/x-ssa",
            "ttml": "application/ttml+xml",
        }.get(format_name.lower(), "text/plain")
