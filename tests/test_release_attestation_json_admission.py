from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.packaging import attestation as attestation_module


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"verificationResult":{},"verificationResult":{"statement":{}}}]',
        b'[{"verificationResult":{"statement":{"subject":[],"subject":[{}]}}}]',
        b'[{"verificationResult":NaN}]',
        b'[{"verificationResult":Infinity}]',
        b'[{"verificationResult":1e400}]',
    ],
)
def test_attestation_verification_reader_rejects_ambiguous_numeric_json(
    tmp_path: Path,
    raw: bytes,
) -> None:
    verification = tmp_path / "verification.json"
    verification.write_bytes(raw)

    with pytest.raises(ValueError, match="invalid JSON"):
        attestation_module._read_verification(verification)


def test_attestation_verification_reader_rejects_oversized_integer(
    tmp_path: Path,
) -> None:
    verification = tmp_path / "verification.json"
    raw = b'[{"verificationResult":' + (b"9" * 1235) + b"}]"
    verification.write_bytes(raw)

    with pytest.raises(ValueError, match="invalid JSON"):
        attestation_module._read_verification(verification)


def test_attestation_verification_reader_accepts_strict_bom_payload(
    tmp_path: Path,
) -> None:
    verification = tmp_path / "verification.json"
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
    verification.write_bytes(
        b"\xef\xbb\xbf" + json.dumps(evidence).encode("utf-8")
    )

    assert attestation_module._read_verification(verification) == evidence
