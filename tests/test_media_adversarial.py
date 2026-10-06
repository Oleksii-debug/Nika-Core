from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.media.contracts import SubtitleKind, SubtitleTrack
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.ffprobe import _classify_ffmpeg_license
from nika_core.media.process import ProcessResult
from nika_core.media.subtitles import SubtitlePolicy, select_subtitle_track
from nika_core.media.yt_dlp import YtDlpAdapter, YtDlpPolicy


class FakeRunner:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd: Path, timeout_seconds: float, env=None) -> ProcessResult:
        del cwd, timeout_seconds, env
        normalized = tuple(argv)
        self.calls.append(normalized)
        return ProcessResult(
            argv=normalized,
            returncode=0,
            stdout=json.dumps(self._payload).encode("utf-8"),
            stderr=b"",
            elapsed_seconds=0.01,
        )


def test_subtitle_policy_finishes_manual_phase_before_automatic() -> None:
    tracks = (
        SubtitleTrack(track_id="auto-uk", language="uk", kind=SubtitleKind.AUTOMATIC),
        SubtitleTrack(track_id="manual-en", language="en", kind=SubtitleKind.MANUAL),
    )
    selected = select_subtitle_track(
        tracks,
        policy=SubtitlePolicy(preferred_languages=("uk", "en")),
    )
    assert selected is not None
    assert selected.track_id == "manual-en"


def test_yt_dlp_remote_locator_and_track_urls_are_redacted_and_ids_are_stable(
    tmp_path: Path,
) -> None:
    payload = {
        "id": "abc",
        "title": "x",
        "duration": 5,
        "webpage_url": "https://user:password@example.test/watch?v=abc&token=secret-value",
        "subtitles": {
            "uk": [
                {
                    "ext": "vtt",
                    "url": "https://caption-user:caption-pass@cdn.test/sub.vtt?signature=secret-signature",
                }
            ]
        },
    }
    first = YtDlpAdapter(FakeRunner(payload)).discover("https://example.test/watch?v=abc", cwd=tmp_path)
    second = YtDlpAdapter(FakeRunner(payload)).discover("https://example.test/watch?v=abc", cwd=tmp_path)
    assert "password" not in first.source.locator
    assert "secret-value" not in first.source.locator
    assert "user@" not in first.source.locator
    assert first.subtitles[0].url is not None
    assert "caption-pass" not in first.subtitles[0].url
    assert "secret-signature" not in first.subtitles[0].url
    assert "caption-user@" not in first.subtitles[0].url
    assert first.subtitles[0].track_id == second.subtitles[0].track_id


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://alice:secret@example.test/watch?v=abc", MediaErrorCode.AUTH_REQUIRED),
        ("https://example.test/watch?token=secret", MediaErrorCode.AUTH_REQUIRED),
        ("https://example.test/watch?signature=secret", MediaErrorCode.AUTH_REQUIRED),
        ("http://localhost/watch", MediaErrorCode.INVALID_SOURCE),
        ("http://127.0.0.1/watch", MediaErrorCode.INVALID_SOURCE),
        ("http://169.254.169.254/latest/meta-data", MediaErrorCode.INVALID_SOURCE),
        ("http://10.0.0.8/watch", MediaErrorCode.INVALID_SOURCE),
        ("http://[::1]/watch", MediaErrorCode.INVALID_SOURCE),
    ],
)
def test_yt_dlp_rejects_credential_and_private_network_urls_before_subprocess(
    tmp_path: Path,
    url: str,
    code: MediaErrorCode,
) -> None:
    runner = FakeRunner({"id": "not-reached"})
    with pytest.raises(MediaError) as caught:
        YtDlpAdapter(runner).discover(url, cwd=tmp_path)
    assert caught.value.code == code
    assert runner.calls == []


def test_yt_dlp_allows_normal_public_query_and_preserves_fixed_argv(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "id": "abc",
            "title": "x",
            "duration": 5,
            "webpage_url": "https://example.test/watch?v=abc",
        }
    )
    url = "https://example.test/watch?v=abc&list=public-list"
    result = YtDlpAdapter(runner).discover(url, cwd=tmp_path)
    assert result.version.upstream_id == "abc"
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[-1] == url
    assert "--ignore-config" in argv
    assert "--skip-download" in argv
    assert "--exec" not in argv
    assert "--netrc-cmd" not in argv
    assert "--write-link" not in argv
    assert "--cookies-from-browser" not in argv


def test_yt_dlp_private_network_requires_explicit_policy_opt_in(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "id": "local-service",
            "title": "x",
            "duration": 5,
            "webpage_url": "http://127.0.0.1/media",
        }
    )
    result = YtDlpAdapter(runner).discover(
        "http://127.0.0.1/media",
        cwd=tmp_path,
        policy=YtDlpPolicy(allow_private_networks=True),
    )
    assert result.version.upstream_id == "local-service"
    assert runner.calls[0][-1] == "http://127.0.0.1/media"


def test_yt_dlp_rejects_oversized_format_catalog(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "id": "abc",
            "title": "x",
            "duration": 5,
            "formats": [{"format_id": "1"}, {"format_id": "2"}],
        }
    )
    with pytest.raises(MediaError) as caught:
        YtDlpAdapter(runner).discover(
            "https://example.test/watch?v=abc",
            cwd=tmp_path,
            policy=YtDlpPolicy(max_formats=1),
        )
    assert caught.value.code == MediaErrorCode.METADATA_LIMIT


def test_yt_dlp_rejects_oversized_subtitle_catalog(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "id": "abc",
            "title": "x",
            "duration": 5,
            "subtitles": {
                "uk": [
                    {"ext": "vtt", "url": "https://cdn.test/one.vtt"},
                    {"ext": "vtt", "url": "https://cdn.test/two.vtt"},
                ]
            },
        }
    )
    with pytest.raises(MediaError) as caught:
        YtDlpAdapter(runner).discover(
            "https://example.test/watch?v=abc",
            cwd=tmp_path,
            policy=YtDlpPolicy(max_subtitle_tracks=1),
        )
    assert caught.value.code == MediaErrorCode.METADATA_LIMIT


def test_ffmpeg_license_classifier_distinguishes_lgpl_gpl_version3_and_nonfree() -> None:
    assert _classify_ffmpeg_license("configuration: --enable-shared") == (
        "LGPL-2.1-or-later/build-dependent"
    )
    assert _classify_ffmpeg_license("configuration: --enable-version3") == (
        "LGPL-3.0-or-later/build-dependent"
    )
    assert _classify_ffmpeg_license("configuration: --enable-gpl") == (
        "GPL-2.0-or-later/build-dependent"
    )
    assert _classify_ffmpeg_license("configuration: --enable-gpl --enable-version3") == (
        "GPL-3.0-or-later/build-dependent"
    )
    assert _classify_ffmpeg_license("configuration: --enable-nonfree") == (
        "NONFREE-UNREDISTRIBUTABLE-REVIEW-REQUIRED"
    )
