from __future__ import annotations

import json
from math import inf, nan
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.ffprobe import FFprobeAdapter


class FakeRunner:
    def __init__(self, payload: object) -> None:
        self.stdout = json.dumps(payload).encode("utf-8")

    def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=self.stdout)


def _probe(tmp_path: Path, payload: object):
    executable = tmp_path / "ffprobe-stub.exe"
    executable.write_bytes(b"fixture executable, never launched")
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"fixture media")
    adapter = FFprobeAdapter(executable, runner=FakeRunner(payload))
    return adapter.probe(media, asset_id="fixture", cwd=tmp_path)


@pytest.mark.parametrize(
    "value", ("NaN", "Infinity", "-Infinity", "1e309", nan, inf, -inf, True)
)
def test_rejects_nonfinite_or_boolean_format_duration(tmp_path: Path, value: object) -> None:
    with pytest.raises(MediaError) as caught:
        _probe(tmp_path, {"format": {"duration": value}, "streams": []})
    assert caught.value.code == MediaErrorCode.PROBE_FAILED


@pytest.mark.parametrize("value", (True, False, inf, nan, 1.5, -1))
def test_rejects_invalid_format_bitrate(tmp_path: Path, value: object) -> None:
    with pytest.raises(MediaError) as caught:
        _probe(tmp_path, {"format": {"bit_rate": value}, "streams": []})
    assert caught.value.code == MediaErrorCode.PROBE_FAILED


@pytest.mark.parametrize("format_value", (None, [], "not-an-object", 123))
def test_rejects_nonobject_format(tmp_path: Path, format_value: object) -> None:
    with pytest.raises(MediaError) as caught:
        _probe(tmp_path, {"format": format_value, "streams": []})
    assert caught.value.code == MediaErrorCode.PROBE_FAILED


@pytest.mark.parametrize("streams", (None, {}, "not-an-array", [None], [{"index": 0}, 3]))
def test_rejects_nonobject_streams_instead_of_dropping_them(
    tmp_path: Path, streams: object
) -> None:
    with pytest.raises(MediaError) as caught:
        _probe(tmp_path, {"format": {}, "streams": streams})
    assert caught.value.code == MediaErrorCode.PROBE_FAILED


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("duration", "Infinity"),
        ("duration", nan),
        ("bit_rate", True),
        ("bit_rate", 1.5),
        ("sample_rate", inf),
        ("channels", -1),
        ("width", "bad"),
        ("height", 1.2),
        ("index", -1),
    ),
)
def test_rejects_invalid_numeric_stream_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(MediaError) as caught:
        _probe(tmp_path, {"format": {}, "streams": [{field: value}]})
    assert caught.value.code == MediaErrorCode.PROBE_FAILED


def test_preserves_valid_metadata_and_optional_absent_fields(tmp_path: Path) -> None:
    result = _probe(
        tmp_path,
        {
            "format": {"format_name": "matroska", "duration": "3.5", "bit_rate": "1250"},
            "streams": [
                {
                    "index": 0,
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                    "duration": "3.5",
                }
            ],
        },
    )
    assert result.container == "matroska"
    assert result.duration_seconds == 3.5
    assert result.bit_rate == 1250
    assert result.streams[0]["sample_rate"] == "48000"

    missing = _probe(tmp_path, {})
    assert missing.duration_seconds is None
    assert missing.streams == ()
