from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nika_core.media.subtitle_acquisition as subtitle_acquisition
from nika_core.media.contracts import (
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    SubtitleKind,
    SubtitleTrack,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file
from nika_core.media.process import ProcessResult
from nika_core.media.subtitle_acquisition import YtDlpSubtitleAcquirer, stable_subtitle_tracks
from nika_core.media.yt_dlp import YtDlpDiscovery


class _StaticDiscovery:
    def __init__(self, discovery: YtDlpDiscovery) -> None:
        self._discovery = discovery

    def discover(self, url: str, *, cwd: Path, policy) -> YtDlpDiscovery:
        del url, cwd, policy
        return self._discovery


class _SubtitleRunner:
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
    ) -> ProcessResult:
        del timeout_seconds, env, cancel_event, watched_paths, max_watched_file_bytes
        normalized = tuple(argv)
        (cwd / "track.uk.vtt").write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\nПривіт\n\n"
            "00:00:02.000 --> 00:00:04.000\nсвіте\n\n"
            "00:00:04.000 --> 00:00:09.000\nNika\n",
            encoding="utf-8",
        )
        return ProcessResult(normalized, 0, b"", b"", 0.01)


class _BarrierSubtitleRunner:
    def __init__(self, payload: str, barrier: threading.Barrier) -> None:
        self._payload = payload
        self._barrier = barrier

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
    ) -> ProcessResult:
        del timeout_seconds, env, cancel_event, watched_paths, max_watched_file_bytes
        normalized = tuple(argv)
        (cwd / "track.uk.vtt").write_text(self._payload, encoding="utf-8")
        self._barrier.wait(timeout=5)
        return ProcessResult(normalized, 0, b"", b"", 0.01)


def _discovery() -> YtDlpDiscovery:
    source = MediaSource(
        source_id="remote:byte-binding",
        kind=MediaSourceKind.REMOTE_MEDIA,
        locator="https://example.test/watch/byte-binding",
    )
    version = MediaVersion(
        version_id="remote-version:byte-binding",
        source_id=source.source_id,
        metadata_sha256="a" * 64,
        duration_seconds=10,
        upstream_id="byte-binding",
    )
    track = SubtitleTrack(
        track_id="raw-track",
        language="uk",
        kind=SubtitleKind.MANUAL,
        format="vtt",
        source_label="subtitles",
        url="https://cdn.example.test/caption.vtt?sig=ephemeral",
    )
    return YtDlpDiscovery(
        source=source,
        version=version,
        subtitles=(track,),
        formats=(),
        sanitized_metadata={},
    )


def _payload(label: str) -> str:
    return (
        "WEBVTT\n\n"
        f"00:00:00.000 --> 00:00:02.000\n{label} one\n\n"
        f"00:00:02.000 --> 00:00:04.000\n{label} two\n\n"
        f"00:00:04.000 --> 00:00:09.000\n{label} three\n"
    )


def _acquire_with_runner(tmp_path: Path, runner: _BarrierSubtitleRunner):
    discovery = _discovery()
    stable_track = stable_subtitle_tracks(discovery)[0]
    return YtDlpSubtitleAcquirer(
        runner=runner,
        discovery=_StaticDiscovery(discovery),
    ).acquire_subtitle(
        discovery.source.locator,
        expected_version=discovery.version,
        expected_track=stable_track,
        output_root=tmp_path,
    )


def test_subtitle_bytes_cannot_change_between_normalization_and_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pysubs2")
    discovery = _discovery()
    stable_track = stable_subtitle_tracks(discovery)[0]
    real_normalize = subtitle_acquisition.normalize_subtitle_file

    def mutate_after_normalize(path: Path, **kwargs):
        transcript = real_normalize(path, **kwargs)
        path.write_bytes(b"attacker replacement bytes")
        return transcript

    monkeypatch.setattr(
        subtitle_acquisition,
        "normalize_subtitle_file",
        mutate_after_normalize,
    )

    with pytest.raises(MediaError) as caught:
        YtDlpSubtitleAcquirer(
            runner=_SubtitleRunner(),
            discovery=_StaticDiscovery(discovery),
        ).acquire_subtitle(
            discovery.source.locator,
            expected_version=discovery.version,
            expected_track=stable_track,
            output_root=tmp_path,
        )

    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert not list(tmp_path.glob("*.subtitle.*"))
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))


def test_concurrent_distinct_subtitle_bytes_keep_separate_hash_bound_assets(tmp_path: Path) -> None:
    pytest.importorskip("pysubs2")
    barrier = threading.Barrier(2)
    runners = (
        _BarrierSubtitleRunner(_payload("alpha"), barrier),
        _BarrierSubtitleRunner(_payload("beta"), barrier),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_acquire_with_runner, tmp_path, runner) for runner in runners]
        results = [future.result(timeout=10) for future in futures]

    assert results[0].asset.sha256 != results[1].asset.sha256
    assert results[0].asset.relative_path != results[1].asset.relative_path
    assert results[0].transcript.segments[0].text == "alpha one"
    assert results[1].transcript.segments[0].text == "beta one"
    for result in results:
        path = tmp_path / result.asset.relative_path
        assert result.asset.sha256 in result.asset.relative_path
        assert sha256_file(path) == result.asset.sha256
        assert path.stat().st_size == result.asset.size_bytes
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob(".*.partial"))
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))


def test_concurrent_identical_subtitle_bytes_reuse_one_verified_asset(tmp_path: Path) -> None:
    pytest.importorskip("pysubs2")
    barrier = threading.Barrier(2)
    payload = _payload("same")
    runners = (
        _BarrierSubtitleRunner(payload, barrier),
        _BarrierSubtitleRunner(payload, barrier),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_acquire_with_runner, tmp_path, runner) for runner in runners]
        results = [future.result(timeout=10) for future in futures]

    assert results[0].asset.sha256 == results[1].asset.sha256
    assert results[0].asset.relative_path == results[1].asset.relative_path
    final_path = tmp_path / results[0].asset.relative_path
    assert sha256_file(final_path) == results[0].asset.sha256
    assert len(list(tmp_path.glob("*.subtitle.vtt"))) == 1
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob(".*.partial"))
    assert not list(tmp_path.glob(".*.subtitle-staging-*"))
