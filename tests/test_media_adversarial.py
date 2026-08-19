from __future__ import annotations

import json
from pathlib import Path

from nika_core.media.contracts import SubtitleKind, SubtitleTrack
from nika_core.media.ffprobe import _classify_ffmpeg_license
from nika_core.media.process import ProcessResult
from nika_core.media.subtitles import SubtitlePolicy, select_subtitle_track
from nika_core.media.yt_dlp import YtDlpAdapter


class FakeRunner:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def run(self, argv, *, cwd: Path, timeout_seconds: float, env=None) -> ProcessResult:
        del cwd, timeout_seconds, env
        normalized = tuple(argv)
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
        "webpage_url": "https://example.test/watch?v=abc&token=secret-value",
        "subtitles": {
            "uk": [
                {
                    "ext": "vtt",
                    "url": "https://cdn.test/sub.vtt?signature=secret-signature",
                }
            ]
        },
    }
    first = YtDlpAdapter(FakeRunner(payload)).discover("https://example.test/watch?v=abc", cwd=tmp_path)
    second = YtDlpAdapter(FakeRunner(payload)).discover("https://example.test/watch?v=abc", cwd=tmp_path)
    assert "secret-value" not in first.source.locator
    assert first.subtitles[0].url is not None
    assert "secret-signature" not in first.subtitles[0].url
    assert first.subtitles[0].track_id == second.subtitles[0].track_id


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
