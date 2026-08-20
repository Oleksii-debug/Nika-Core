from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nika_core.media.contracts import (
    AssetKind,
    MediaAsset,
    SubtitleKind,
    SubtitleTrack,
    Transcript,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.files import promote_partial_file
from nika_core.media.hashing import sha256_json
from nika_core.media.process import ProcessResult, SafeProcessRunner
from nika_core.media.subtitles import SubtitlePolicy, normalize_subtitle_file
from nika_core.media.yt_dlp import YtDlpAdapter, YtDlpPolicy


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


@dataclass(frozen=True, slots=True)
class RemoteAcquisitionPolicy:
    timeout_seconds: float = 30 * 60
    max_bytes: int = 2 * 1024 * 1024 * 1024
    allow_private_networks: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 24 * 60 * 60:
            raise ValueError("timeout_seconds must be between 0 and 86400")
        if self.max_bytes <= 0 or self.max_bytes > 128 * 1024 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 byte and 128 GiB")


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionPolicy:
    timeout_seconds: float = 5 * 60
    max_bytes: int = 32 * 1024 * 1024
    allow_private_networks: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60 * 60:
            raise ValueError("timeout_seconds must be between 0 and 3600")
        if self.max_bytes <= 0 or self.max_bytes > 256 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 byte and 256 MiB")


@dataclass(frozen=True, slots=True)
class RemoteAcquisitionResult:
    asset: MediaAsset
    resumed_partial: bool


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionResult:
    asset: MediaAsset
    transcript: Transcript
    resumed_partial: bool


class YtDlpRemoteAcquirer:
    """Acquire remote media evidence from stable source-page identity."""

    def __init__(self, runner: ProcessRunnerPort | None = None) -> None:
        self._runner = runner or SafeProcessRunner(max_output_bytes=4 * 1024 * 1024)

    def acquire_media(
        self,
        source_url: str,
        *,
        version_id: str,
        output_root: Path,
        format_id: str | None = None,
        expected_sha256: str | None = None,
        policy: RemoteAcquisitionPolicy | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RemoteAcquisitionResult:
        active_policy = policy or RemoteAcquisitionPolicy()
        self._validate_source_url(
            source_url,
            allow_private_networks=active_policy.allow_private_networks,
        )
        self._validate_version_id(version_id)
        selected_format = self._validate_format_id(format_id)
        root = self._validated_root(output_root)

        name_basis = {"version_id": version_id, "format_id": selected_format or "best"}
        stem = f"remote-{sha256_json(name_basis)[:32]}"
        partial_path = root / f"{stem}.media.partial"
        resume_path = Path(f"{partial_path}.part")
        final_path = root / f"{stem}.media"
        self._validate_output_state(
            partial_path=partial_path,
            resume_path=resume_path,
            final_path=final_path,
            max_bytes=active_policy.max_bytes,
            label="media",
        )
        resumed_partial = resume_path.exists()

        argv = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--continue",
            "--no-write-info-json",
            "--no-write-comments",
            "--no-write-thumbnail",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--max-filesize",
            str(active_policy.max_bytes),
            "-o",
            str(partial_path),
        ]
        if selected_format is not None:
            argv.extend(("-f", selected_format))
        argv.append(source_url)

        self._run_bounded(
            argv,
            root=root,
            timeout_seconds=active_policy.timeout_seconds,
            max_bytes=active_policy.max_bytes,
            partial_path=partial_path,
            resume_path=resume_path,
            cancel_event=cancel_event,
        )
        if not partial_path.is_file():
            raise MediaError(
                MediaErrorCode.PROCESS_FAILED,
                "yt-dlp completed without producing the expected media partial",
            )
        promoted = promote_partial_file(
            partial_path,
            final_path,
            allowed_root=root,
            expected_sha256=expected_sha256,
            max_bytes=active_policy.max_bytes,
        )
        relative_path = promoted.path.resolve(strict=True).relative_to(root).as_posix()
        asset_id = (
            "remote-asset:"
            f"{sha256_json({'version_id': version_id, 'sha256': promoted.sha256})[:32]}"
        )
        return RemoteAcquisitionResult(
            asset=MediaAsset(
                asset_id=asset_id,
                version_id=version_id,
                kind=AssetKind.ORIGINAL,
                relative_path=relative_path,
                sha256=promoted.sha256,
                size_bytes=promoted.size_bytes,
                media_type="application/octet-stream",
                immutable_original=True,
            ),
            resumed_partial=resumed_partial,
        )

    def acquire_subtitle(
        self,
        source_url: str,
        *,
        version_id: str,
        track: SubtitleTrack,
        output_root: Path,
        media_duration_seconds: float | None,
        expected_sha256: str | None = None,
        policy: SubtitleAcquisitionPolicy | None = None,
        quality_policy: SubtitlePolicy | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SubtitleAcquisitionResult:
        """Rediscover one selected subtitle and publish it only after quality validation.

        ``track.url`` is discovery metadata only and is intentionally never executed.
        The stable source page, selected language and manual/automatic kind are passed
        back to yt-dlp so expired extractor/CDN URLs do not become durable credentials.
        """
        active_policy = policy or SubtitleAcquisitionPolicy()
        self._validate_source_url(
            source_url,
            allow_private_networks=active_policy.allow_private_networks,
        )
        self._validate_version_id(version_id)
        language = self._validate_subtitle_language(track.language)
        if track.kind not in {SubtitleKind.MANUAL, SubtitleKind.AUTOMATIC}:
            raise MediaError(
                MediaErrorCode.INVALID_SUBTITLE,
                "translated subtitle acquisition requires a separately proven translation path",
            )
        root = self._validated_root(output_root)
        name_basis = {
            "version_id": version_id,
            "track_id": track.track_id,
            "language": language,
            "kind": track.kind.value,
        }
        stem = f"subtitle-{sha256_json(name_basis)[:32]}"
        partial_base = root / f"{stem}.subtitle.partial"
        partial_path = root / f"{stem}.subtitle.partial.vtt"
        resume_path = Path(f"{partial_path}.part")
        final_path = root / f"{stem}.subtitle.vtt"
        self._validate_output_state(
            partial_path=partial_path,
            resume_path=resume_path,
            final_path=final_path,
            max_bytes=active_policy.max_bytes,
            label="subtitle",
        )
        resumed_partial = resume_path.exists()

        subtitle_flags = (
            ("--write-subs", "--no-write-auto-subs")
            if track.kind == SubtitleKind.MANUAL
            else ("--no-write-subs", "--write-auto-subs")
        )
        argv = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--continue",
            "--skip-download",
            *subtitle_flags,
            "--sub-langs",
            language,
            "--sub-format",
            "vtt",
            "-o",
            f"subtitle:{partial_base}.%(ext)s",
            source_url,
        ]
        self._run_bounded(
            argv,
            root=root,
            timeout_seconds=active_policy.timeout_seconds,
            max_bytes=active_policy.max_bytes,
            partial_path=partial_path,
            resume_path=resume_path,
            cancel_event=cancel_event,
        )
        if not partial_path.is_file():
            raise MediaError(
                MediaErrorCode.INVALID_SUBTITLE,
                "selected subtitle was not produced; rediscover subtitle tracks before retry",
                retryable=True,
            )

        transcript = normalize_subtitle_file(
            partial_path,
            track=track,
            version_id=version_id,
            media_duration_seconds=media_duration_seconds,
            policy=quality_policy,
        )
        promoted = promote_partial_file(
            partial_path,
            final_path,
            allowed_root=root,
            expected_sha256=expected_sha256,
            max_bytes=active_policy.max_bytes,
        )
        relative_path = promoted.path.resolve(strict=True).relative_to(root).as_posix()
        asset_id = (
            "subtitle-asset:"
            f"{sha256_json({'track_id': track.track_id, 'sha256': promoted.sha256})[:32]}"
        )
        return SubtitleAcquisitionResult(
            asset=MediaAsset(
                asset_id=asset_id,
                version_id=version_id,
                kind=AssetKind.SUBTITLE,
                relative_path=relative_path,
                sha256=promoted.sha256,
                size_bytes=promoted.size_bytes,
                media_type="text/vtt",
                immutable_original=True,
            ),
            transcript=transcript,
            resumed_partial=resumed_partial,
        )

    def _run_bounded(
        self,
        argv: list[str],
        *,
        root: Path,
        timeout_seconds: float,
        max_bytes: int,
        partial_path: Path,
        resume_path: Path,
        cancel_event: threading.Event | None,
    ) -> ProcessResult:
        try:
            return self._runner.run(
                argv,
                cwd=root,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                watched_paths=(partial_path, resume_path),
                max_watched_file_bytes=max_bytes,
            )
        except MediaError:
            self._enforce_partial_bound(resume_path, max_bytes=max_bytes)
            raise

    @staticmethod
    def _validate_output_state(
        *,
        partial_path: Path,
        resume_path: Path,
        final_path: Path,
        max_bytes: int,
        label: str,
    ) -> None:
        if final_path.exists():
            raise FileExistsError(f"refusing to overwrite existing {label} output: {final_path.name}")
        if partial_path.exists():
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                f"completed partial {label} already exists; reconcile it before retry",
            )
        if resume_path.exists() and resume_path.stat().st_size > max_bytes:
            raise MediaError(
                MediaErrorCode.SOURCE_TOO_LARGE,
                f"resumable {label} partial already exceeds the configured byte limit",
            )

    @staticmethod
    def _validated_root(output_root: Path) -> Path:
        root = output_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("output_root must be a directory")
        return root

    @staticmethod
    def _validate_source_url(source_url: str, *, allow_private_networks: bool) -> None:
        YtDlpAdapter._validate_source_url(
            source_url,
            policy=YtDlpPolicy(allow_private_networks=allow_private_networks),
        )

    @staticmethod
    def _validate_version_id(version_id: str) -> None:
        if not isinstance(version_id, str) or not version_id.strip() or len(version_id) > 160:
            raise ValueError("version_id must be non-empty text up to 160 characters")
        if any(char in version_id for char in ("\x00", "\r", "\n")):
            raise ValueError("version_id contains invalid characters")

    @staticmethod
    def _validate_format_id(format_id: str | None) -> str | None:
        if format_id is None:
            return None
        value = format_id.strip()
        if not value or len(value) > 120:
            raise ValueError("format_id must be non-empty text up to 120 characters")
        if value.startswith("-"):
            raise ValueError("format_id must not look like a command-line option")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-/,[]()")
        if any(char not in allowed for char in value):
            raise ValueError("format_id contains unsupported characters")
        return value

    @staticmethod
    def _validate_subtitle_language(language: str) -> str:
        value = language.strip()
        if not value or len(value) > 80 or value.startswith("-"):
            raise ValueError("subtitle language must be a bounded positive language tag")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if any(char not in allowed for char in value):
            raise ValueError("subtitle language contains regex or delimiter characters")
        return value

    @staticmethod
    def _enforce_partial_bound(path: Path, *, max_bytes: int) -> None:
        if path.exists() and path.stat().st_size > max_bytes:
            try:
                path.unlink()
            except OSError:
                pass
            raise MediaError(
                MediaErrorCode.SOURCE_TOO_LARGE,
                "media partial exceeded the configured byte limit",
            )
