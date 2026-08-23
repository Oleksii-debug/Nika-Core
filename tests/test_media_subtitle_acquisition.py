from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from nika_core.media.contracts import (
    AssetKind,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    StructuredMediaArtifact,
    SubtitleKind,
    SubtitleTrack,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.handoff import build_corpus_media_handoff
from nika_core.media.hashing import sha256_file
from nika_core.media.process import ProcessResult
from nika_core.media.subtitle_acquisition import (
    SubtitleAcquisitionPolicy,
    YtDlpSubtitleAcquirer,
    stable_subtitle_track,
    stable_subtitle_tracks,
)
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
    def __init__(self, payload: str | None = None, *, oversized_bytes: int | None = None) -> None:
        self.payload = payload or (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\nПривіт\n\n"
            "00:00:02.000 --> 00:00:04.000\nсвіте\n\n"
            "00:00:04.000 --> 00:00:09.000\nNika\n"
        )
        self.oversized_bytes = oversized_bytes
        self.calls: list[
            tuple[tuple[str, ...], Path, float, threading.Event | None, dict[str, str] | None]
        ] = []

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
        del watched_paths, max_watched_file_bytes
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout_seconds, cancel_event, env))
        target = cwd / "track.uk.vtt"
        if self.oversized_bytes is not None:
            target.write_bytes(b"x" * self.oversized_bytes)
        else:
            target.write_text(self.payload, encoding="utf-8")
        return ProcessResult(normalized, 0, b"", b"", 0.01)


class CancellingRunner:
    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        (cwd / "track.uk.vtt.part").write_bytes(b"bounded-partial")
        raise MediaError(MediaErrorCode.PROCESS_CANCELLED, "cancelled")


def discovery_for(
    track: SubtitleTrack,
    *,
    version_id: str = "remote-version:one",
    metadata_sha256: str = "a" * 64,
    url: str = "https://example.test/watch/42",
    subtitles: tuple[SubtitleTrack, ...] | None = None,
) -> YtDlpDiscovery:
    source = MediaSource(
        source_id="remote:one",
        kind=MediaSourceKind.REMOTE_MEDIA,
        locator=url,
    )
    version = MediaVersion(
        version_id=version_id,
        source_id=source.source_id,
        metadata_sha256=metadata_sha256,
        duration_seconds=10,
        upstream_id="42",
    )
    return YtDlpDiscovery(
        source=source,
        version=version,
        subtitles=(track,) if subtitles is None else subtitles,
        formats=(),
        sanitized_metadata={},
    )


def require_subtitle_parser() -> None:
    pytest.importorskip(
        "pysubs2",
        reason="subtitle parsing is an explicit optional [media] component",
    )


def initial_and_fresh(
    *,
    kind: SubtitleKind = SubtitleKind.MANUAL,
    payload_url: str = "https://cdn.example.test/caption.vtt?expire=1&sig=old-secret",
) -> tuple[YtDlpDiscovery, YtDlpDiscovery, SubtitleTrack]:
    raw = SubtitleTrack(
        track_id="ephemeral-discovery-id",
        language="uk",
        kind=kind,
        format="vtt",
        name="Ukrainian",
        source_label="subtitles" if kind == SubtitleKind.MANUAL else "automatic_captions",
        url=payload_url,
    )
    initial = discovery_for(raw)
    fresh_raw = raw.model_copy(
        update={
            "track_id": "new-ephemeral-discovery-id",
            "url": "https://cdn.example.test/caption.vtt?expire=2&sig=new-secret",
        }
    )
    fresh = discovery_for(fresh_raw)
    stable = stable_subtitle_tracks(initial)[0]
    return initial, fresh, stable


def test_stable_track_identity_ignores_ephemeral_url_and_discovery_ordinal_id() -> None:
    _initial, fresh, expected = initial_and_fresh()
    rediscovered = stable_subtitle_tracks(fresh)[0]
    assert expected.track_id.startswith("subtitle-track-stable:")
    assert rediscovered.track_id == expected.track_id
    assert expected.url is None
    assert rediscovered.url is None
    assert "secret" not in expected.model_dump_json()
    assert stable_subtitle_track(expected) == expected


def test_ambiguous_durable_track_identity_fails_closed() -> None:
    first = SubtitleTrack(
        track_id="first",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
        name="same",
        source_label="subtitles",
        url="https://cdn.example.test/a",
    )
    second = first.model_copy(
        update={"track_id": "second", "url": "https://cdn.example.test/b"}
    )
    with pytest.raises(MediaError) as caught:
        stable_subtitle_tracks(discovery_for(first, subtitles=(first, second)))
    assert caught.value.code == MediaErrorCode.INVALID_METADATA


def test_ambiguous_cli_materialization_selector_fails_before_download(tmp_path: Path) -> None:
    initial, fresh, expected_track = initial_and_fresh()
    second = fresh.subtitles[0].model_copy(
        update={
            "track_id": "alternate-raw-id",
            "name": "Alternate Ukrainian",
            "url": "https://cdn.example.test/alternate.vtt?sig=other-secret",
        }
    )
    ambiguous = YtDlpDiscovery(
        source=fresh.source,
        version=fresh.version,
        subtitles=(fresh.subtitles[0], second),
        formats=(),
        sanitized_metadata={},
    )
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as caught:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(ambiguous)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
        )
    assert caught.value.code == MediaErrorCode.INVALID_METADATA
    assert runner.calls == []


def test_manual_subtitle_rediscovery_hardening_hash_promotion_and_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_subtitle_parser()
    monkeypatch.setenv("NIKA_FAKE_TOKEN", "must-not-reach-child")
    initial, fresh, expected_track = initial_and_fresh()
    runner = SubtitleWritingRunner()
    result = YtDlpSubtitleAcquirer(
        runner=runner,
        discovery=StaticDiscovery(fresh),
    ).acquire_subtitle(
        initial.source.locator,
        expected_version=initial.version,
        expected_track=expected_track,
        output_root=tmp_path,
        policy=SubtitleAcquisitionPolicy(max_bytes=4096, download_timeout_seconds=12),
    )

    assert result.asset.kind == AssetKind.SUBTITLE
    assert result.asset.immutable_original is True
    final_path = tmp_path / result.asset.relative_path
    assert final_path.is_file()
    assert result.asset.sha256 == sha256_file(final_path)
    assert result.transcript.source_track_id == expected_track.track_id
    assert result.transcript.version_id == initial.version.version_id
    assert result.rediscovered_track == expected_track
    assert result.rediscovered_track.url is None
    segment_ids = [segment.segment_id for segment in result.transcript.segments]
    assert len(segment_ids) == len(set(segment_ids))

    argv, cwd, timeout_seconds, _cancel, env = runner.calls[0]
    assert cwd.name.startswith(".remote-")
    assert timeout_seconds == 12
    assert argv[:3] == (sys.executable, "-m", "yt_dlp")
    for option in (
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-cache-dir",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--no-playlist",
        "--skip-download",
        "--write-subs",
    ):
        assert option in argv
    assert "--cookies" not in argv
    assert "--cookies-from-browser" not in argv
    assert "--plugin-dirs" not in argv
    assert "--config-locations" not in argv
    assert env is not None
    assert env["YTDLP_NO_PLUGINS"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONSAFEPATH"] == "1"
    assert "NIKA_FAKE_TOKEN" not in env
    assert argv[-1] == initial.source.locator
    joined = " ".join(argv)
    assert "cdn.example.test" not in joined
    assert "old-secret" not in joined
    assert "new-secret" not in joined
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))

    artifact = StructuredMediaArtifact(
        artifact_id="artifact:subtitle",
        version_id=initial.version.version_id,
        source=initial.source,
        version=initial.version,
        assets=(result.asset,),
        transcript=result.transcript,
    )
    handoff = build_corpus_media_handoff(artifact)
    assert handoff.version_id == initial.version.version_id
    assert [block.text for block in handoff.blocks] == ["Привіт", "світе", "Nika"]


def test_exact_metadata_version_drift_fails_even_when_version_id_is_unchanged(
    tmp_path: Path,
) -> None:
    initial, fresh, expected_track = initial_and_fresh()
    drifted = YtDlpDiscovery(
        source=fresh.source,
        version=fresh.version.model_copy(update={"metadata_sha256": "b" * 64}),
        subtitles=fresh.subtitles,
        formats=fresh.formats,
        sanitized_metadata=fresh.sanitized_metadata,
    )
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as caught:
        YtDlpSubtitleAcquirer(
            runner=runner,
            discovery=StaticDiscovery(drifted),
        ).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
        )
    assert caught.value.code == MediaErrorCode.INVALID_METADATA
    assert runner.calls == []


def test_track_disappearance_and_track_mutation_fail_before_materialization(
    tmp_path: Path,
) -> None:
    initial, fresh, expected_track = initial_and_fresh()
    runner = SubtitleWritingRunner()
    missing = YtDlpDiscovery(
        source=fresh.source,
        version=fresh.version,
        subtitles=(),
        formats=(),
        sanitized_metadata={},
    )
    with pytest.raises(MediaError) as disappeared:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(missing)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
        )
    assert disappeared.value.code == MediaErrorCode.INVALID_SUBTITLE
    assert runner.calls == []

    changed_raw = fresh.subtitles[0].model_copy(update={"format": "srt"})
    changed = YtDlpDiscovery(
        source=fresh.source,
        version=fresh.version,
        subtitles=(changed_raw,),
        formats=(),
        sanitized_metadata={},
    )
    with pytest.raises(MediaError) as mutated:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(changed)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
        )
    assert mutated.value.code == MediaErrorCode.INVALID_SUBTITLE
    assert runner.calls == []


def test_ephemeral_track_authority_and_credential_like_source_are_rejected(
    tmp_path: Path,
) -> None:
    initial, fresh, expected_track = initial_and_fresh()
    raw_with_url = expected_track.model_copy(
        update={"url": "https://cdn.example.test/replay.vtt?sig=secret"}
    )
    runner = SubtitleWritingRunner()
    with pytest.raises(ValueError, match="persistence-safe"):
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(fresh)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=raw_with_url,
            output_root=tmp_path,
        )
    with pytest.raises(MediaError) as credential:
        YtDlpSubtitleAcquirer(runner, StaticDiscovery(fresh)).acquire_subtitle(
            "https://example.test/watch/42?token=secret",
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
        )
    assert credential.value.code == MediaErrorCode.AUTH_REQUIRED
    assert runner.calls == []


def test_cancel_and_oversize_cleanup_leave_no_replayable_staging(tmp_path: Path) -> None:
    initial, fresh, expected_track = initial_and_fresh()
    cancelling = CancellingRunner()
    with pytest.raises(MediaError) as cancelled:
        YtDlpSubtitleAcquirer(cancelling, StaticDiscovery(fresh)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
            policy=SubtitleAcquisitionPolicy(max_bytes=64),
        )
    assert cancelled.value.code == MediaErrorCode.PROCESS_CANCELLED
    assert cancelling.calls == 1
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))
    assert not list(tmp_path.glob("*.partial"))

    oversized = SubtitleWritingRunner(oversized_bytes=65)
    with pytest.raises(MediaError) as too_large:
        YtDlpSubtitleAcquirer(oversized, StaticDiscovery(fresh)).acquire_subtitle(
            initial.source.locator,
            expected_version=initial.version,
            expected_track=expected_track,
            output_root=tmp_path,
            policy=SubtitleAcquisitionPolicy(max_bytes=64),
        )
    assert too_large.value.code == MediaErrorCode.SOURCE_TOO_LARGE
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))
    assert not list(tmp_path.glob("*.partial"))


def test_manual_and_automatic_tracks_follow_different_quality_policy(tmp_path: Path) -> None:
    require_subtitle_parser()
    one_segment = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:09.000\nsingle usable segment\n"
    )
    manual_initial, manual_fresh, manual_track = initial_and_fresh(kind=SubtitleKind.MANUAL)
    manual = YtDlpSubtitleAcquirer(
        SubtitleWritingRunner(one_segment),
        StaticDiscovery(manual_fresh),
    ).acquire_subtitle(
        manual_initial.source.locator,
        expected_version=manual_initial.version,
        expected_track=manual_track,
        output_root=tmp_path,
    )
    assert len(manual.transcript.segments) == 1

    auto_root = tmp_path / "auto"
    auto_root.mkdir()
    auto_initial, auto_fresh, auto_track = initial_and_fresh(kind=SubtitleKind.AUTOMATIC)
    auto_runner = SubtitleWritingRunner(one_segment)
    with pytest.raises(MediaError) as low_quality:
        YtDlpSubtitleAcquirer(
            auto_runner,
            StaticDiscovery(auto_fresh),
        ).acquire_subtitle(
            auto_initial.source.locator,
            expected_version=auto_initial.version,
            expected_track=auto_track,
            output_root=auto_root,
        )
    assert low_quality.value.code == MediaErrorCode.LOW_QUALITY_SUBTITLE
    auto_argv = auto_runner.calls[0][0]
    assert "--write-auto-subs" in auto_argv
    assert "--write-subs" not in auto_argv
    assert not list(auto_root.glob("*.subtitle.*"))
    assert not list(auto_root.glob(".*.subtitle-staging-*"))
