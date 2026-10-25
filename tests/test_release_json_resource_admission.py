from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    _MAX_PREHUMAN_EVIDENCE_BYTES,
    _decode_release_manifest,
    _read_evidence_object,
    verify_distributable_evidence,
    verify_release_archive,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRODUCT_VERSION = "0.0.2"
ARTIFACT_REFERENCE = "./dist/NikaCore-0.0.2-windows-x64.zip"


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


def test_prehuman_json_reader_rejects_integer_above_digit_limit(tmp_path: Path) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_bytes(b'{"value":' + b"9" * 1235 + b"}")

    assert _read_evidence_object(path) is None


def test_prehuman_json_reader_enforces_integer_bit_boundary(tmp_path: Path) -> None:
    path = tmp_path / "m12-prehuman-evidence.json"
    accepted = 1 << 4095
    path.write_text('{"value":' + str(accepted) + "}", encoding="utf-8")
    assert _read_evidence_object(path) == {"value": accepted}

    path.write_text('{"value":' + str(1 << 4096) + "}", encoding="utf-8")
    assert _read_evidence_object(path) is None


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


def _public_distributable_findings(artifact: Path, evidence: Path) -> tuple[str, ...]:
    return verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=ARTIFACT_REFERENCE,
        expected_product_version=PRODUCT_VERSION,
    )


def test_public_distributable_verifier_maps_oversize_to_invalid_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "NikaCore.zip"
    artifact.write_bytes(b"artifact")
    evidence = tmp_path / "m12-prehuman-evidence.json"
    evidence.write_bytes(b" " * (_MAX_PREHUMAN_EVIDENCE_BYTES + 1))

    assert _public_distributable_findings(artifact, evidence) == (
        "distributable:invalid-evidence",
    )


def test_public_distributable_verifier_maps_deep_json_to_invalid_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "NikaCore.zip"
    artifact.write_bytes(b"artifact")
    evidence = tmp_path / "m12-prehuman-evidence.json"
    evidence.write_text(
        '{"nested":' + "[" * 64 + "0" + "]" * 64 + "}",
        encoding="utf-8",
    )

    assert _public_distributable_findings(artifact, evidence) == (
        "distributable:invalid-evidence",
    )


def test_public_release_archive_maps_deep_manifest_to_invalid_manifest(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "NikaCore.zip"
    deep_manifest = '{"nested":' + "[" * 64 + "0" + "]" * 64 + "}"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", deep_manifest)

    assert verify_release_archive(
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=PRODUCT_VERSION,
    ) == ("archive:invalid-manifest",)


@pytest.mark.parametrize(
    "member_path",
    (
        "safe\u202efile.txt",
        "Cafe\u0301.txt",
        "safe\u0085file.txt",
        "\ud800.txt",
    ),
)
def test_public_release_archive_rejects_noncanonical_unicode_path(
    tmp_path: Path,
    member_path: str,
) -> None:
    artifact = tmp_path / "NikaCore.zip"
    manifest = {
        "manifest_version": 2,
        "product": "Nika Core",
        "version": PRODUCT_VERSION,
        "source_sha": SOURCE_SHA,
        "files": [
            {
                "path": member_path,
                "size": 0,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }
        ],
    }
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=True))

    assert verify_release_archive(
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=PRODUCT_VERSION,
    ) == ("archive:manifest:path:0",)
