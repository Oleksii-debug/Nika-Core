from __future__ import annotations

import hashlib

import pytest

from nika_core.product_command.contracts import EvidenceReference
from nika_core.product_command.reference_safety import safe_evidence_reference


class _DeceptiveReference(str):
    def encode(self, *args, **kwargs) -> bytes:
        return b"health://decoy/safe"

    def strip(self, *args, **kwargs) -> str:
        return "health://decoy/safe"

    def casefold(self) -> str:
        return "health://decoy/safe"

    def __str__(self) -> str:
        return "health://decoy/safe"


def test_public_evidence_canonicalizes_str_subclass_before_sensitivity_checks() -> None:
    raw = "credential://provider/project-1/raw-secret"
    reference = _DeceptiveReference(raw)
    expected = "evidence-sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    assert safe_evidence_reference(reference) == expected

    presented = EvidenceReference(kind="test", reference=reference, label="Evidence")
    assert presented.reference == expected
    assert type(presented.reference) is str


def test_public_evidence_uses_real_subclass_payload_for_utf8_validation() -> None:
    malformed = _DeceptiveReference("evidence://\ud800")

    with pytest.raises(ValueError, match="valid UTF-8"):
        safe_evidence_reference(malformed)


def test_public_evidence_returns_detached_builtin_str_for_benign_subclass() -> None:
    raw = "health://project-1/service-api/healthy"
    reference = _DeceptiveReference(raw)

    sanitized = safe_evidence_reference(reference)

    assert sanitized == raw
    assert type(sanitized) is str


@pytest.mark.parametrize(
    "reference",
    (
        "//operator:raw-password@service.invalid/evidence",
        "%2F%2Foperator%3Araw-password%40service.invalid%2Fevidence",
        "//[invalid-host/evidence",
    ),
)
def test_public_evidence_hashes_network_path_userinfo(reference: str) -> None:
    protected = safe_evidence_reference(reference)

    assert protected.startswith("evidence-sha256:")
    assert reference not in protected
