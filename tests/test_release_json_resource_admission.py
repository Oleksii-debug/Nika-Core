from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.packaging.release import (
    _MAX_PREHUMAN_EVIDENCE_BYTES,
    _decode_release_manifest,
    _read_evidence_object,
)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"identity":"first","identity":"second"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e400}',
        b'{"value":"\xff"}',
        b"\xff",
    ],
)
def test_prehuman_json_reader_rejects_ambiguous_or_invalid_input(
    tmp_path: Path,
    raw: bytes,
) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_bytes(raw)

    assert _read_evidence_object(path) is None


def test_prehuman_json_reader_rejects_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_bytes(b" " * (_MAX_PREHUMAN_EVIDENCE_BYTES + 1))

    assert _read_evidence_object(path) is None


def test_prehuman_json_reader_accepts_exact_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    prefix = b'{"padding":"'
    suffix = b'"}'
    padding = b"x" * (_MAX_PREHUMAN_EVIDENCE_BYTES - len(prefix) - len(suffix))
    path.write_bytes(prefix + padding + suffix)

    payload = _read_evidence_object(path)

    assert payload == {"padding": padding.decode("ascii")}


def test_prehuman_json_reader_rejects_excess_depth(tmp_path: Path) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_text('{"nested":' + "[" * 64 + "0" + "]" * 64 + "}", encoding="utf-8")

    assert _read_evidence_object(path) is None


def test_prehuman_json_reader_accepts_exact_depth_and_quoted_brackets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_text(
        '{"quoted":"[{}]\\\"","nested":' + "[" * 63 + "0" + "]" * 63 + "}",
        encoding="utf-8",
    )

    payload = _read_evidence_object(path)

    assert isinstance(payload, dict)
    assert payload["quoted"] == '[{}]"'


def test_release_manifest_decoder_rejects_excess_depth() -> None:
    raw = ('{"nested":' + "[" * 64 + "0" + "]" * 64 + "}").encode("utf-8")

    assert _decode_release_manifest(raw) is None


@pytest.mark.parametrize(
    "raw",
    (
        b'{"manifest_version":NaN}',
        b'{"manifest_version":Infinity}',
        b'{"manifest_version":1e400}',
    ),
)
def test_release_manifest_decoder_rejects_nonfinite_numbers(raw: bytes) -> None:
    assert _decode_release_manifest(raw) is None
