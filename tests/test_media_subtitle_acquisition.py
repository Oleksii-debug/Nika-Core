from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nika_core.media.contracts import (
    AssetKind,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    SubtitleKind,
    SubtitleTrack,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_json
from nika_core.media.process import ProcessResult
from nika_core.media.subtitle_acquisition import (
    SubtitleAcquisitionPolicy,
    YtDlpSubtitleAcquirer,
    stable_subtitle_tracks,
)
from nika_core.media.subtitles import SubtitlePolicy
from nika_core.media.yt_dlp import YtDlpDiscovery


class StaticDiscovery:
    def __init__(self, discovery: YtDlpDiscovery) -> None:
        self.discovery = discovery
        self.calls: list[tuple[str, Path]] = []

    def discover(self, url: str, *, cwd: Path, policy) -> YtDlpDiscovery:
        del policy
        self.calls.append((url, cwd))
        return self.discovery


class SubtitleWritingRunner:
    def __init__(self, payload: str | None = None) -> None:
        self.payload = payload or (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\nПривіт\n\n"
            "00:00:02.000 --> 00:00:04.000\nсвіте\n\n"
            "00:00:04.000 --> 00:00:09.000\nNika\n"
        )
        self.calls: list[tuple[tuple[str, ...], Path, float, threading.Event | None]] = []

    def run(
        self,
        argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        env=None,
        cancel_event: threading.Event | None = None,
        watched_paths=(),
        max_watched_file_bytes=None,
    ) -> ProcessResult:
        del env, watched_paths, max_watched_file_bytes
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout_seconds, cancel_event))
        (cwd / "track.uk.vtt").write_text(self.payload, encoding="utf-8")
        return ProcessResult(normalized, 0, b"", b"", 0.01)


class FailingRunner:
    def run(
        self,
        argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        env=None,
        cancel_event=None,
        watched_paths=(),
        max_watched_file_bytes=None,
    ):
        del argv, timeout_seconds, env, cancel_event, watched_paths, max_watched_file_bytes
        (cwd / "track.uk.vtt.part").write_bytes(b"bounded-partial")
        raise MediaError(MediaErrorCode.PROCESS_CANCELLED, "cancelled")


def discovery_for(
    track: SubtitleTrack,
    *,
    version_id: str = "remote-version:one",
    subtitles: tuple[SubtitleTrack, ...] | None = None,
) -> YtDlpDiscovery:
    source = MediaSource(
        source_id="remote:one",
        kind=MediaSourceKind.REMOTE_MEDIA,
        locator="https://example.test/watch/42",
    )
    version = MediaVersion(
        version_id=version_id,
        source_id=source.source_id,
        metadata_sha256="a" * 64,
        duration_seconds=10,
    )
    return YtDlpDiscovery(
        source=source,
        version=version,
        subtitles=(track,) if subtitles is None else subtitles,
        formats=(),
        sanitized_metadata={},
    )


def test_stable_track_projection_drops_ephemeral_signed_locator() -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:stable",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
        url="https://cdn.example.test/caption.vtt?expire=1&sig=secret",
    )
    safe = stable_subtitle_tracks(discovery_for(raw))[0]
    assert safe.track_id == raw.track_id
    assert safe.url is None
    assert "secret" not in safe.model_dump_json()


def test_manual_subtitle_is_rediscovered_downloaded_and_normalized(tmp_path: Path) -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:stable",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
        url="https://signed-cdn.test/caption.vtt?expires=123&signature=private",
    )
    discovery = StaticDiscovery(discovery_for(raw))
    runner = SubtitleWritingRunner()
    result = YtDlpSubtitleAcquirer(runner, discovery).acquire_subtitle(
        "https://example.test/watch/42",
        version_id="remote-version:one",
        track_id=raw.track_id,
        output_root=tmp_path,
        media_duration_seconds=10,
        policy=SubtitleAcquisitionPolicy(max_bytes=4096, download_timeout_seconds=12),
    )

    assert result.asset.kind == AssetKind.SUBTITLE
    assert result.asset.immutable_original is True
    assert result.asset.relative_path.endswith(".subtitle.vtt")
    assert result.transcript.method.value == "platform_subtitle"
    assert result.transcript.source_track_id == raw.track_id
    assert result.rediscovered_track.url is None
    assert result.transcript.segments[0].text == "Привіт"

    argv, cwd, timeout_seconds, _cancel = runner.calls[0]
    assert cwd.name.endswith(".subtitle-staging")
    assert timeout_seconds == 12
    assert "--write-subs" in argv
    assert "--write-auto-subs" not in argv
    assert argv[argv.index("--sub-langs") + 1] == "uk"
    assert argv[argv.index("--sub-format") + 1] == "vtt"
    assert argv[-1] == "https://example.test/watch/42"
    assert "signed-cdn.test" not in " ".join(argv)
    assert "signature=private" not in " ".join(argv)
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob(".*.subtitle-staging"))


def test_automatic_subtitle_keeps_quality_gate_and_auto_flag(tmp_path: Path) -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:auto",
        language="uk",
        kind=SubtitleKind.AUTOMATIC,
        format="vtt",
        url="https://signed-cdn.test/auto.vtt?expires=1",
    )
    runner = SubtitleWritingRunner()
    result = YtDlpSubtitleAcquirer(runner, StaticDiscovery(discovery_for(raw))).acquire_subtitle(
        "https://example.test/watch/42",
        version_id="remote-version:one",
        track_id=raw.track_id,
        output_root=tmp_path,
        media_duration_seconds=10,
        subtitle_policy=SubtitlePolicy(automatic_min_coverage_ratio=0.5),
    )
    argv = runner.calls[0][0]
    assert "--write-auto-subs" in argv
    assert "--write-subs" not in argv
    assert len(result.transcript.segments) == 3


def test_version_drift_and_disappeared_track_fail_before_download(tmp_path: Path) -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:stable",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
    )
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as drift:
        YtDlpSubtitleAcquirer(
            runner,
            StaticDiscovery(discovery_for(raw, version_id="remote-version:new")),
        ).acquire_subtitle(
            "https://example.test/watch/42",
            version_id="remote-version:old",
            track_id=raw.track_id,
            output_root=tmp_path,
            media_duration_seconds=10,
        )
    assert drift.value.code == MediaErrorCode.INVALID_METADATA
    assert runner.calls == []

    missing = discovery_for(raw, subtitles=())
    with pytest.raises(MediaError) as disappeared:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(missing)).acquire_subtitle(
            "https://example.test/watch/42",
            version_id="remote-version:one",
            track_id=raw.track_id,
            output_root=tmp_path,
            media_duration_seconds=10,
        )
    assert disappeared.value.code == MediaErrorCode.INVALID_SUBTITLE
    assert runner.calls == []


def test_cancel_keeps_only_bounded_staging_and_retry_can_reconcile(tmp_path: Path) -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:stable",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
    )
    discovery = StaticDiscovery(discovery_for(raw))
    with pytest.raises(MediaError) as cancelled:
        YtDlpSubtitleAcquirer(FailingRunner(), discovery).acquire_subtitle(
            "https://example.test/watch/42",
            version_id="remote-version:one",
            track_id=raw.track_id,
            output_root=tmp_path,
            media_duration_seconds=10,
            policy=SubtitleAcquisitionPolicy(max_bytes=64),
        )
    assert cancelled.value.code == MediaErrorCode.PROCESS_CANCELLED
    leftovers = list(tmp_path.glob(".*.subtitle-staging/*.part"))
    assert len(leftovers) == 1
    assert leftovers[0].stat().st_size <= 64


def test_oversized_existing_staging_fails_before_download(tmp_path: Path) -> None:
    raw = SubtitleTrack(
        track_id="subtitle-track:stable",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
    )
    stable_stem = "remote-" + sha256_json(
        {"version_id": "remote-version:one", "track_id": raw.track_id}
    )[:32]
    staging = tmp_path / f".{stable_stem}.subtitle-staging"
    staging.mkdir()
    (staging / "track.uk.vtt.part").write_bytes(b"12345")
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as oversized:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(discovery_for(raw))).acquire_subtitle(
            "https://example.test/watch/42",
            version_id="remote-version:one",
            track_id=raw.track_id,
            output_root=tmp_path,
            media_duration_seconds=10,
            policy=SubtitleAcquisitionPolicy(max_bytes=4),
        )
    assert oversized.value.code == MediaErrorCode.SOURCE_TOO_LARGE
    assert runner.calls == []
