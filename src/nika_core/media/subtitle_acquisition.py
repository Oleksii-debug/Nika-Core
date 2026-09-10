from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nika_core.media.contracts import (
    AssetKind,
    MediaAsset,
    MediaVersion,
    SubtitleKind,
    SubtitleTrack,
    Transcript,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.files import PromotedFile, promote_partial_file
from nika_core.media.hashing import sha256_file, sha256_json
from nika_core.media.process import ProcessResult, SafeProcessRunner
from nika_core.media.subtitles import SubtitlePolicy, normalize_subtitle_file
from nika_core.media.yt_dlp import YtDlpAdapter, YtDlpDiscovery, YtDlpPolicy

_HARDENED_YT_DLP_ARGS = (
    "--ignore-config",
    "--no-plugin-dirs",
    "--no-cache-dir",
    "--no-cookies",
    "--no-cookies-from-browser",
)
_FORBIDDEN_YT_DLP_OPTIONS = frozenset(
    {
        "--config-locations",
        "--cookies",
        "--cookies-from-browser",
        "--exec",
        "--external-downloader",
        "--netrc",
        "--netrc-cmd",
        "--netrc-location",
        "--password",
        "--plugin-dirs",
        "--twofactor",
        "--use-postprocessor",
        "--username",
        "-2",
        "-n",
        "-p",
        "-u",
    }
)
_SAFE_ENV_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_SUBTITLE_LANGUAGE_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_SUBTITLE_FORMAT_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_STABLE_TRACK_PREFIX = "subtitle-track-stable:"


class ProcessRunnerPort(Protocol):
    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        watched_paths: tuple[Path, ...] = (),
        max_watched_file_bytes: int | None = None,
    ) -> ProcessResult: ...


class SubtitleDiscoveryPort(Protocol):
    def discover(
        self,
        url: str,
        *,
        cwd: Path,
        policy: YtDlpPolicy | None = None,
        privacy: str = "private",
        auth_ref: str | None = None,
    ) -> YtDlpDiscovery: ...


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPolicy:
    discovery_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 120.0
    max_bytes: int = 16 * 1024 * 1024
    max_staging_files: int = 8
    allow_private_networks: bool = False

    def __post_init__(self) -> None:
        if self.discovery_timeout_seconds <= 0 or self.discovery_timeout_seconds > 600:
            raise ValueError("discovery_timeout_seconds must be between 0 and 600")
        if self.download_timeout_seconds <= 0 or self.download_timeout_seconds > 3600:
            raise ValueError("download_timeout_seconds must be between 0 and 3600")
        if self.max_bytes < 1 or self.max_bytes > 64 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 64 MiB")
        if self.max_staging_files < 1 or self.max_staging_files > 64:
            raise ValueError("max_staging_files must be between 1 and 64")


@dataclass(frozen=True, slots=True)
class RemoteSubtitleResult:
    asset: MediaAsset
    transcript: Transcript
    rediscovered_version: MediaVersion
    rediscovered_track: SubtitleTrack


def stable_subtitle_track(track: SubtitleTrack) -> SubtitleTrack:
    """Return minimal persistence-safe authority, excluding ephemeral/cosmetic fields."""

    canonical_source_label = _canonical_source_label(track.kind)
    basis = {
        "kind": track.kind.value,
        "language": _normalize_language(track.language),
        "format": track.format.strip().lower(),
        "source_label": canonical_source_label,
    }
    return track.model_copy(
        update={
            "track_id": f"{_STABLE_TRACK_PREFIX}{sha256_json(basis)[:32]}",
            "name": "",
            "url": None,
            "is_default": False,
            "source_label": canonical_source_label,
        }
    )


def stable_subtitle_tracks(discovery: YtDlpDiscovery) -> tuple[SubtitleTrack, ...]:
    """Project discovery into durable track identities and reject ambiguous collisions."""

    projected = tuple(stable_subtitle_track(track) for track in discovery.subtitles)
    identities = [track.track_id for track in projected]
    if len(identities) != len(set(identities)):
        raise MediaError(
            MediaErrorCode.INVALID_METADATA,
            "subtitle catalog contains ambiguous durable track identities",
        )
    return projected


class _HardenedYtDlpRunner:
    """Force a non-interactive, config/plugin/credential-free yt-dlp subprocess surface."""

    def __init__(self, delegate: ProcessRunnerPort) -> None:
        self._delegate = delegate

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        watched_paths: tuple[Path, ...] = (),
        max_watched_file_bytes: int | None = None,
    ) -> ProcessResult:
        normalized = tuple(str(part) for part in argv)
        expected_prefix = (sys.executable, "-m", "yt_dlp")
        if normalized[:3] != expected_prefix:
            raise ValueError("subtitle acquisition runner only accepts python -m yt_dlp")
        for part in normalized[3:]:
            option = part.split("=", 1)[0]
            if option in _FORBIDDEN_YT_DLP_OPTIONS:
                raise ValueError(f"forbidden yt-dlp option: {option}")
        remaining = tuple(part for part in normalized[3:] if part not in _HARDENED_YT_DLP_ARGS)
        hardened_argv = expected_prefix + _HARDENED_YT_DLP_ARGS + remaining
        source_env = os.environ if env is None else env
        hardened_env = {
            key: value
            for key, value in source_env.items()
            if key.upper() in _SAFE_ENV_KEYS
        }
        hardened_env["PYTHONNOUSERSITE"] = "1"
        hardened_env["PYTHONSAFEPATH"] = "1"
        hardened_env["YTDLP_NO_PLUGINS"] = "1"
        return self._delegate.run(
            hardened_argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=hardened_env,
            cancel_event=cancel_event,
            watched_paths=watched_paths,
            max_watched_file_bytes=max_watched_file_bytes,
        )


class _StagingMonitor:
    def __init__(
        self,
        staging: Path,
        *,
        root: Path,
        max_bytes: int,
        max_files: int,
        external_cancel: threading.Event | None,
    ) -> None:
        self.staging = staging
        self.root = root
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.external_cancel = external_cancel
        self.cancel_event = threading.Event()
        self.failure: MediaError | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._inspect()
        if self.external_cancel is not None and self.external_cancel.is_set():
            self.cancel_event.set()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._inspect()

    def _watch(self) -> None:
        while not self._stop.wait(0.01):
            if self.external_cancel is not None and self.external_cancel.is_set():
                self.cancel_event.set()
            try:
                self._inspect()
            except MediaError as exc:
                self.failure = exc
                self.cancel_event.set()
                return

    def _inspect(self) -> None:
        _validated_staging_files(
            self.staging,
            root=self.root,
            max_bytes=self.max_bytes,
            max_files=self.max_files,
        )


class YtDlpSubtitleAcquirer:
    """Materialize a platform subtitle only from exact stable media/track authority."""

    def __init__(
        self,
        runner: ProcessRunnerPort | None = None,
        discovery: SubtitleDiscoveryPort | None = None,
    ) -> None:
        base_runner = runner or SafeProcessRunner(max_output_bytes=4 * 1024 * 1024)
        self._runner = _HardenedYtDlpRunner(base_runner)
        self._discovery = discovery or YtDlpAdapter(self._runner)

    def acquire_subtitle(
        self,
        source_url: str,
        *,
        expected_version: MediaVersion,
        expected_track: SubtitleTrack,
        output_root: Path,
        subtitle_policy: SubtitlePolicy | None = None,
        policy: SubtitleAcquisitionPolicy | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RemoteSubtitleResult:
        active = policy or SubtitleAcquisitionPolicy()
        root = output_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("output_root must be a directory")
        expected_track = self._validate_expected_track(expected_track)
        YtDlpAdapter._validate_source_url(
            source_url,
            policy=YtDlpPolicy(allow_private_networks=active.allow_private_networks),
        )

        fresh = self._discovery.discover(
            source_url,
            cwd=root,
            policy=YtDlpPolicy(
                timeout_seconds=active.discovery_timeout_seconds,
                allow_private_networks=active.allow_private_networks,
            ),
        )
        self._require_exact_version(expected_version, fresh.version)
        fresh_tracks = stable_subtitle_tracks(fresh)
        refreshed = next(
            (candidate for candidate in fresh_tracks if candidate.track_id == expected_track.track_id),
            None,
        )
        if refreshed is None:
            raise MediaError(
                MediaErrorCode.INVALID_SUBTITLE,
                "selected durable subtitle track disappeared after rediscovery",
            )
        if refreshed != expected_track:
            raise MediaError(
                MediaErrorCode.INVALID_SUBTITLE,
                "selected durable subtitle track changed after rediscovery",
            )
        if refreshed.kind not in {SubtitleKind.MANUAL, SubtitleKind.AUTOMATIC}:
            raise MediaError(
                MediaErrorCode.UNSUPPORTED_SOURCE,
                "translated subtitle materialization is not supported by this acquisition path",
            )
        self._require_unique_materialization_selector(fresh.subtitles, refreshed)
        self._validate_language(refreshed.language)
        self._validate_format(refreshed.format)

        version_fingerprint = self._version_fingerprint(expected_version)
        stable_stem = (
            "remote-"
            f"{sha256_json({'version': version_fingerprint, 'track_id': refreshed.track_id})[:32]}"
        )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{stable_stem}.subtitle-staging-",
                dir=root,
            )
        )
        partial_path: Path | None = None
        monitor = _StagingMonitor(
            staging,
            root=root,
            max_bytes=active.max_bytes,
            max_files=active.max_staging_files,
            external_cancel=cancel_event,
        )
        try:
            monitor.start()
            argv = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                "--no-write-info-json",
                "--no-write-comments",
                "--no-write-thumbnail",
                "--sub-langs",
                refreshed.language,
                "--sub-format",
                refreshed.format,
                "--max-filesize",
                str(active.max_bytes),
                "--write-subs"
                if refreshed.kind == SubtitleKind.MANUAL
                else "--write-auto-subs",
                "-o",
                str(staging / "track"),
                source_url,
            ]
            try:
                self._runner.run(
                    argv,
                    cwd=staging,
                    timeout_seconds=active.download_timeout_seconds,
                    cancel_event=monitor.cancel_event,
                )
            except MediaError as exc:
                if monitor.failure is not None:
                    raise monitor.failure from exc
                raise
            finally:
                monitor.stop()
            if monitor.failure is not None:
                raise monitor.failure

            files = _validated_staging_files(
                staging,
                root=root,
                max_bytes=active.max_bytes,
                max_files=active.max_staging_files,
            )
            candidates = [
                path
                for path in files
                if not path.name.endswith((".part", ".ytdl"))
            ]
            if len(files) != 1 or len(candidates) != 1:
                raise MediaError(
                    MediaErrorCode.PROCESS_FAILED,
                    "yt-dlp must produce exactly one completed subtitle file",
                )
            candidate = candidates[0]
            if candidate.stat().st_size == 0:
                raise MediaError(
                    MediaErrorCode.INVALID_SUBTITLE,
                    "downloaded subtitle file is empty",
                )
            expected_suffix = f".{refreshed.format.lower()}"
            if candidate.suffix.lower() != expected_suffix:
                raise MediaError(
                    MediaErrorCode.INVALID_SUBTITLE,
                    "downloaded subtitle format did not match rediscovered track identity",
                )

            attempt_token = staging.name.rsplit("-", 1)[-1]
            partial_path = root / f".{stable_stem}-{attempt_token}.partial"
            os.replace(candidate, partial_path)
            shutil.rmtree(staging)
            source_sha256 = sha256_file(partial_path, max_bytes=active.max_bytes)
            source_size_bytes = partial_path.stat().st_size
            transcript = normalize_subtitle_file(
                partial_path,
                track=refreshed,
                version_id=expected_version.version_id,
                media_duration_seconds=expected_version.duration_seconds,
                policy=subtitle_policy,
            )
            normalized_sha256 = sha256_file(partial_path, max_bytes=active.max_bytes)
            if normalized_sha256 != source_sha256:
                raise MediaError(
                    MediaErrorCode.CHECKSUM_MISMATCH,
                    "subtitle bytes changed while normalization evidence was being created",
                )
            expected_transcript_id = f"subtitle:{source_sha256[:32]}"
            if transcript.transcript_id != expected_transcript_id:
                raise MediaError(
                    MediaErrorCode.CHECKSUM_MISMATCH,
                    "normalized transcript is not bound to the acquired subtitle bytes",
                )

            final_path = root / (
                f"{stable_stem}-{source_sha256}.subtitle.{refreshed.format.lower()}"
            )
            try:
                promoted = promote_partial_file(
                    partial_path,
                    final_path,
                    allowed_root=root,
                    expected_sha256=source_sha256,
                    max_bytes=active.max_bytes,
                )
            except FileExistsError:
                promoted = _reuse_content_addressed_file(
                    final_path,
                    root=root,
                    expected_sha256=source_sha256,
                    expected_size_bytes=source_size_bytes,
                    max_bytes=active.max_bytes,
                )
                partial_path.unlink(missing_ok=True)

            asset = MediaAsset(
                asset_id=(
                    "subtitle-asset:"
                    f"{sha256_json({'version': version_fingerprint, 'track_id': refreshed.track_id, 'sha256': promoted.sha256})[:32]}"
                ),
                version_id=expected_version.version_id,
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
                rediscovered_version=fresh.version,
                rediscovered_track=refreshed,
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if partial_path is not None and partial_path.exists():
                partial_path.unlink()

    @staticmethod
    def _validate_expected_track(track: SubtitleTrack) -> SubtitleTrack:
        projected = stable_subtitle_track(track)
        if projected != track:
            raise ValueError(
                "expected_track must be canonical persistence-safe subtitle authority"
            )
        return track

    @staticmethod
    def _require_exact_version(expected: MediaVersion, fresh: MediaVersion) -> None:
        if YtDlpSubtitleAcquirer._version_fingerprint(expected) != (
            YtDlpSubtitleAcquirer._version_fingerprint(fresh)
        ):
            raise MediaError(
                MediaErrorCode.INVALID_METADATA,
                "remote media version changed during subtitle rediscovery",
            )

    @staticmethod
    def _require_unique_materialization_selector(
        tracks: tuple[SubtitleTrack, ...],
        expected: SubtitleTrack,
    ) -> None:
        selector = _materialization_selector(expected)
        matches = [track for track in tracks if _materialization_selector(track) == selector]
        if len(matches) != 1:
            raise MediaError(
                MediaErrorCode.INVALID_METADATA,
                "subtitle materialization selector is ambiguous after rediscovery",
            )

    @staticmethod
    def _version_fingerprint(version: MediaVersion) -> dict[str, object]:
        return {
            "version_id": version.version_id,
            "source_id": version.source_id,
            "metadata_sha256": version.metadata_sha256,
            "content_sha256": version.content_sha256,
            "upstream_id": version.upstream_id,
            "duration_seconds": version.duration_seconds,
        }

    @staticmethod
    def _validate_language(value: str) -> None:
        if (
            not value
            or not value[0].isalnum()
            or len(value) > 40
            or any(character not in _SUBTITLE_LANGUAGE_ALLOWED for character in value)
        ):
            raise ValueError("subtitle language contains unsupported characters")

    @staticmethod
    def _validate_format(value: str) -> None:
        if (
            not value
            or not value[0].isalnum()
            or len(value) > 40
            or any(character not in _SUBTITLE_FORMAT_ALLOWED for character in value)
        ):
            raise ValueError("subtitle format contains unsupported characters")

    @staticmethod
    def _media_type(format_name: str) -> str:
        return {
            "vtt": "text/vtt",
            "srt": "application/x-subrip",
            "ass": "text/x-ssa",
            "ssa": "text/x-ssa",
            "ttml": "application/ttml+xml",
        }.get(format_name.lower(), "text/plain")


def _canonical_source_label(kind: SubtitleKind) -> str:
    return {
        SubtitleKind.MANUAL: "subtitles",
        SubtitleKind.AUTOMATIC: "automatic_captions",
        SubtitleKind.TRANSLATED: "translated_subtitles",
    }[kind]


def _normalize_language(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _materialization_selector(track: SubtitleTrack) -> tuple[SubtitleKind, str, str]:
    return (
        track.kind,
        _normalize_language(track.language),
        track.format.strip().lower(),
    )


def _reuse_content_addressed_file(
    path: Path,
    *,
    root: Path,
    expected_sha256: str,
    expected_size_bytes: int,
    max_bytes: int,
) -> PromotedFile:
    if path.is_symlink():
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            "existing subtitle asset must not be a symbolic link",
        )
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            "existing subtitle asset escapes the allowed root",
        ) from exc
    if not resolved.is_file():
        raise MediaError(
            MediaErrorCode.INVALID_SOURCE,
            "existing subtitle asset must be a regular file",
        )
    if resolved.stat().st_size != expected_size_bytes:
        raise MediaError(
            MediaErrorCode.CHECKSUM_MISMATCH,
            "content-addressed subtitle asset size does not match acquired bytes",
        )
    checksum = sha256_file(resolved, max_bytes=max_bytes)
    if checksum != expected_sha256:
        raise MediaError(
            MediaErrorCode.CHECKSUM_MISMATCH,
            "content-addressed subtitle asset does not match acquired bytes",
        )
    return PromotedFile(
        path=resolved,
        sha256=checksum,
        size_bytes=expected_size_bytes,
    )


def _validated_staging_files(
    staging: Path,
    *,
    root: Path,
    max_bytes: int,
    max_files: int,
) -> tuple[Path, ...]:
    if staging.is_symlink():
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            "subtitle staging directory must not be a symbolic link",
        )
    resolved = staging.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            "subtitle staging directory escapes the allowed root",
        ) from exc
    if not resolved.is_dir():
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            "subtitle staging path must be a directory",
        )
    entries = tuple(sorted(resolved.iterdir()))
    if len(entries) > max_files:
        raise MediaError(
            MediaErrorCode.OUTPUT_LIMIT,
            "subtitle staging produced too many files",
        )
    total_bytes = 0
    for path in entries:
        if path.is_symlink():
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                "subtitle staging contains a symbolic link",
            )
        candidate = path.resolve(strict=True)
        try:
            candidate.relative_to(resolved)
        except ValueError as exc:
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                "subtitle staging entry escapes the staging directory",
            ) from exc
        if not candidate.is_file():
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                "subtitle staging contains an unexpected non-file entry",
            )
        size = candidate.stat().st_size
        total_bytes += size
        if size > max_bytes or total_bytes > max_bytes:
            raise MediaError(
                MediaErrorCode.SOURCE_TOO_LARGE,
                "subtitle staging output exceeds configured byte limit",
            )
    return entries
