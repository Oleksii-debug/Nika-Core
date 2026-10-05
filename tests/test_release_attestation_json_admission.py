from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.packaging.attestation import _read_verification


@pytest.mark.parametrize(
    "raw",
    [
        b'{"verificationResult": {"subject": []}}',
        b'[{"verificationResult": {}, "verificationResult": {"statement": {}}}]',
        b'[{"verificationResult": {"statement": {"subject": [], "subject": [{}]}}}]',
        b'[{"verificationResult": {"statement": {"digest": {"sha256": "a", "sha256": "b"}}}}]',
        b'[{"verificationResult": NaN}]',
        b'[{"verificationResult": Infinity}]',
        b'[{"verificationResult": 1e400}]',
        b'[{"verificationResult": "\xff"}]',
        b'\xff',
        b'[]',
    ],
)
def test_attestation_verification_rejects_invalid_or_ambiguous_json(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "verification.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="attestation verification output"):
        _read_verification(path)


def test_attestation_verification_rejects_large_input_before_json_decode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verification.json"
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="invalid or oversized JSON"):
        _read_verification(path)


def test_attestation_verification_rejects_deep_recursive_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verification.json"
    path.write_text("[" * 3000 + "{}" + "]" * 3000, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or oversized JSON"):
        _read_verification(path)


def test_attestation_verification_accepts_bom_and_standard_gh_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verification.json"
    evidence = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [{"digest": {"sha256": "a" * 64}}],
                }
            }
        }
    ]
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(evidence).encode("utf-8"))
    assert _read_verification(path) == evidence


def test_attestation_verification_accepts_exact_byte_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verification.json"
    path.write_bytes(b"[" + b" " * (2 * 1024 * 1024 - 3) + b"{}]")
    assert _read_verification(path) == [{}]
