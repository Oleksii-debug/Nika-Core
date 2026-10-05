from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.contracts import SubtitleKind, SubtitleTrack
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.subtitles import (
    SubtitlePolicy,
    _normalize_text,
    normalize_subtitle_file,
)


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


@pytest.mark.parametrize("duration", (float("nan"), float("inf"), -1.0, True, "10", 10**1000))
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


@pytest.mark.parametrize(
    ("override", "field"),
    (
        ({"automatic_min_segments": True}, "automatic_min_segments"),
        ({"automatic_min_segments": 0}, "automatic_min_segments"),
        ({"automatic_min_coverage_ratio": float("nan")}, "automatic_min_coverage_ratio"),
        ({"automatic_min_coverage_ratio": float("inf")}, "automatic_min_coverage_ratio"),
        ({"automatic_min_coverage_ratio": True}, "automatic_min_coverage_ratio"),
        ({"automatic_min_coverage_ratio": 1.1}, "automatic_min_coverage_ratio"),
        ({"automatic_min_coverage_ratio": 10**1000}, "automatic_min_coverage_ratio"),
        ({"automatic_max_malformed_ratio": -0.1}, "automatic_max_malformed_ratio"),
        ({"automatic_max_malformed_ratio": float("nan")}, "automatic_max_malformed_ratio"),
    ),
)
def test_invalid_quality_policy_cannot_bypass_automatic_admission(
    override: dict[str, object], field: str
) -> None:
    with pytest.raises(ValueError, match=field):
        SubtitlePolicy(**override)  # type: ignore[arg-type]


def test_valid_quality_policy_boundary_ratios_remain_available() -> None:
    policy = SubtitlePolicy(
        automatic_min_segments=1,
        automatic_min_coverage_ratio=0,
        automatic_max_malformed_ratio=1,
    )
    assert policy.automatic_min_coverage_ratio == 0
    assert policy.automatic_max_malformed_ratio == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("1 < 2 > 0", "1 < 2 > 0"),
        ("{Україна} <i>відео</i>", "{Україна} відео"),
        (r"{\i1}субтитр{\i0}", "субтитр"),
        ("<00:01.250>час <00:00:02.500>", "час"),
        ("Текст <c.green>зелений</c>", "Текст зелений"),
    ),
)
def test_subtitle_normalization_strips_formatting_without_losing_literal_text(
    raw: str, expected: str
) -> None:
    assert _normalize_text(raw) == expected
