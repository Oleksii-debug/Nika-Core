from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.media.acquisition import (
    SubtitleAcquisitionPolicy,
    YtDlpRemoteAcquirer,
)
from nika_core.media.contracts import (
    AssetKind,
    SubtitleKind,
    SubtitleTrack,
    TranscriptMethod,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.process import ProcessResult


VTT = b"""WEBVTT

00:00:00.000 --> 00:00:01.000
Hello

00:00:01.000 --> 00:00:02.000
from

00:00:02.000 --> 00:00:03.000
Nika
"""


class SubtitleWritingRunner:
    def __init__(self, payload: bytes = VTT) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []
        self.watched: list[tuple[tuple[Path, ...], int | None]] = []

    def run(
        self,
        argv,
        *,
        cwd,
        timeout_seconds,
        env=None,
        cancel_event=None,
        watched_paths=(),
        max_watched_file_bytes=None,
    ) -> ProcessResult:
        normalized = tuple(argv)
        self.calls.append(normalized)
        self.watched.append((watched_paths, max_watched_file_bytes))
        template = normalized[normalized.index("-o") + 1]
        assert template.startswith("subtitle:")
        output = Path(template.removeprefix("subtitle:").replace("%(ext)s", "vtt"))
        output.write_bytes(self.payload)
        return ProcessResult(normalized, 0, b"", b"", 0.01)


def track(*, kind: SubtitleKind = SubtitleKind.MANUAL, language: str = "uk") -> SubtitleTrack:
    return SubtitleTrack(
        track_id=f"track:{kind.value}:{language}",
        language=language,
        kind=kind,
        name="Selected subtitle",
        url="https://cdn.example.invalid/sub.vtt?token=do-not-use",
        format="vtt",
        source_label="fixture",
    )


def test_manual_subtitle_is_rediscovered_from_stable_source_and_normalized(
    tmp_path: Path,
) -> None:
    runner = SubtitleWritingRunner()
    result = YtDlpRemoteAcquirer(runner).acquire_subtitle(
        "https://example.com/watch/42",
        version_id="version:one",
        track=track(),
        output_root=tmp_path,
        media_duration_seconds=3.0,
        expected_sha256=hashlib.sha256(VTT).hexdigest(),
        policy=SubtitleAcquisitionPolicy(max_bytes=1024, timeout_seconds=20),
    )

    assert result.asset.kind == AssetKind.SUBTITLE
    assert result.asset.immutable_original is True
    assert result.asset.media_type == "text/vtt"
    assert result.transcript.method == TranscriptMethod.PLATFORM_SUBTITLE
    assert [segment.text for segment in result.transcript.segments] == ["Hello", "from", "Nika"]
    assert (tmp_path / result.asset.relative_path).read_bytes() == VTT
    assert not list(tmp_path.glob("*.partial.vtt"))

    argv = runner.calls[0]
    assert argv[-1] == "https://example.com/watch/42"
    assert not any("cdn.example.invalid" in value for value in argv)
    assert not any("do-not-use" in value for value in argv)
    assert "--skip-download" in argv
    assert "--write-subs" in argv
    assert "--no-write-auto-subs" in argv
    assert argv[argv.index("--sub-langs") + 1] == "uk"
    assert argv[argv.index("--sub-format") + 1] == "vtt"
    watched_paths, watched_limit = runner.watched[0]
    assert watched_limit == 1024
    assert len(watched_paths) == 2


def test_automatic_subtitle_uses_only_automatic_caption_flag(tmp_path: Path) -> None:
    runner = SubtitleWritingRunner()
    result = YtDlpRemoteAcquirer(runner).acquire_subtitle(
        "https://example.com/watch/42",
        version_id="version:auto",
        track=track(kind=SubtitleKind.AUTOMATIC, language="en-US"),
        output_root=tmp_path,
        media_duration_seconds=3.0,
    )
    assert len(result.transcript.segments) == 3
    argv = runner.calls[0]
    assert "--write-auto-subs" in argv
    assert "--no-write-subs" in argv
    assert argv[argv.index("--sub-langs") + 1] == "en-US"


@pytest.mark.parametrize("language", ["en.*,all", "-all", "uk,en", "en$"])
def test_subtitle_language_regex_or_exclusion_syntax_is_rejected_before_runner(
    tmp_path: Path,
    language: str,
) -> None:
    runner = SubtitleWritingRunner()
    with pytest.raises(ValueError, match="subtitle language"):
        YtDlpRemoteAcquirer(runner).acquire_subtitle(
            "https://example.com/watch/42",
            version_id="v1",
            track=track(language=language),
            output_root=tmp_path,
            media_duration_seconds=3.0,
        )
    assert runner.calls == []


def test_translated_track_requires_separate_translation_path(tmp_path: Path) -> None:
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as caught:
        YtDlpRemoteAcquirer(runner).acquire_subtitle(
            "https://example.com/watch/42",
            version_id="v1",
            track=track(kind=SubtitleKind.TRANSLATED),
            output_root=tmp_path,
            media_duration_seconds=3.0,
        )
    assert caught.value.code == MediaErrorCode.INVALID_SUBTITLE
    assert runner.calls == []


def test_missing_selected_subtitle_requires_rediscovery(tmp_path: Path) -> None:
    class NoOutputRunner:
        def run(
            self,
            argv,
            *,
            cwd,
            timeout_seconds,
            env=None,
            cancel_event=None,
            watched_paths=(),
            max_watched_file_bytes=None,
        ):
            return ProcessResult(tuple(argv), 0, b"", b"", 0.01)

    with pytest.raises(MediaError) as caught:
        YtDlpRemoteAcquirer(NoOutputRunner()).acquire_subtitle(
            "https://example.com/watch/42",
            version_id="v1",
            track=track(),
            output_root=tmp_path,
            media_duration_seconds=3.0,
        )
    assert caught.value.code == MediaErrorCode.INVALID_SUBTITLE
    assert caught.value.retryable is True


def test_low_quality_automatic_subtitle_is_not_published(tmp_path: Path) -> None:
    payload = b"WEBVTT\n\n00:00:00.000 --> 00:00:00.100\nOnly one\n"
    runner = SubtitleWritingRunner(payload)
    with pytest.raises(MediaError) as caught:
        YtDlpRemoteAcquirer(runner).acquire_subtitle(
            "https://example.com/watch/42",
            version_id="v1",
            track=track(kind=SubtitleKind.AUTOMATIC),
            output_root=tmp_path,
            media_duration_seconds=30.0,
        )
    assert caught.value.code == MediaErrorCode.LOW_QUALITY_SUBTITLE
    assert list(tmp_path.glob("*.subtitle.vtt")) == []
    assert len(list(tmp_path.glob("*.subtitle.partial.vtt"))) == 1


def test_subtitle_checksum_mismatch_keeps_unpublished_evidence(tmp_path: Path) -> None:
    runner = SubtitleWritingRunner()
    with pytest.raises(MediaError) as caught:
        YtDlpRemoteAcquirer(runner).acquire_subtitle(
            "https://example.com/watch/42",
            version_id="v1",
            track=track(),
            output_root=tmp_path,
            media_duration_seconds=3.0,
            expected_sha256="0" * 64,
        )
    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert list(tmp_path.glob("*.subtitle.vtt")) == []
    assert len(list(tmp_path.glob("*.subtitle.partial.vtt"))) == 1
