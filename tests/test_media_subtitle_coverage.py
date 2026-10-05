from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.contracts import SubtitleKind, SubtitleTrack
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.subtitles import SubtitlePolicy, normalize_subtitle_file


def _normalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intervals: tuple[tuple[int, int], ...],
    *,
    duration: float | None,
    kind: SubtitleKind = SubtitleKind.AUTOMATIC,
    minimum_coverage: float = 0.5,
):
    events = [
        SimpleNamespace(start=start, end=end, text=f"Репліка {index}")
        for index, (start, end) in enumerate(intervals)
    ]
    monkeypatch.setitem(
        sys.modules,
        "pysubs2",
        SimpleNamespace(load=lambda _path, encoding: events),
    )
    source = tmp_path / "українські субтитри.vtt"
    source.write_text("fixture", encoding="utf-8")
    return normalize_subtitle_file(
        source,
        track=SubtitleTrack(track_id="captions", language="uk", kind=kind),
        version_id="video-1",
        media_duration_seconds=duration,
        policy=SubtitlePolicy(automatic_min_coverage_ratio=minimum_coverage),
    )


def test_sparse_automatic_cues_do_not_turn_the_entire_span_into_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MediaError) as caught:
        _normalize(
            tmp_path,
            monkeypatch,
            ((0, 1000), (10_000, 11_000), (90_000, 91_000)),
            duration=100,
        )
    assert caught.value.code is MediaErrorCode.LOW_QUALITY_SUBTITLE


def test_overlapping_automatic_cues_count_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intervals = ((0, 3000), (1000, 4000), (2000, 5000))
    with pytest.raises(MediaError) as caught:
        _normalize(tmp_path, monkeypatch, intervals, duration=8, minimum_coverage=0.75)
    assert caught.value.code is MediaErrorCode.LOW_QUALITY_SUBTITLE
    transcript = _normalize(
        tmp_path, monkeypatch, intervals, duration=8, minimum_coverage=0.6
    )
    assert len(transcript.segments) == 3


def test_out_of_media_cues_do_not_count_towards_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MediaError) as caught:
        _normalize(
            tmp_path,
            monkeypatch,
            ((0, 1000), (8000, 11_000), (12_000, 100_000)),
            duration=10,
        )
    assert caught.value.code is MediaErrorCode.LOW_QUALITY_SUBTITLE


def test_contiguous_automatic_cues_remain_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _normalize(
        tmp_path,
        monkeypatch,
        ((0, 1000), (1000, 2000), (2000, 9000)),
        duration=10,
    )
    assert [segment.text for segment in transcript.segments] == [
        "Репліка 0",
        "Репліка 1",
        "Репліка 2",
    ]


def test_manual_and_unknown_duration_do_not_invent_coverage_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sparse = ((0, 1000), (10_000, 11_000), (90_000, 91_000))
    assert len(
        _normalize(
            tmp_path, monkeypatch, sparse, duration=100, kind=SubtitleKind.MANUAL
        ).segments
    ) == 3
    assert len(_normalize(tmp_path, monkeypatch, sparse, duration=None).segments) == 3


@pytest.mark.parametrize("duration", (float("nan"), float("inf"), -1.0, True, "10"))
def test_invalid_automatic_media_duration_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duration: object
) -> None:
    with pytest.raises(MediaError) as caught:
        _normalize(
            tmp_path,
            monkeypatch,
            ((0, 1000), (1000, 2000), (2000, 3000)),
            duration=duration,  # type: ignore[arg-type]
        )
    assert caught.value.code is MediaErrorCode.INVALID_SUBTITLE


def test_negative_subtitle_timestamp_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _normalize(
        tmp_path,
        monkeypatch,
        ((-1, 100), (100, 200)),
        duration=1,
        kind=SubtitleKind.MANUAL,
    )
    assert len(result.segments) == 1
    assert result.segments[0].start_ms == 100
