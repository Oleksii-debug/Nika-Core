from __future__ import annotations

from urllib.parse import quote

import pytest

from nika_core.product_command.contracts import EvidenceReference
from nika_core.product_command.reference_safety import safe_evidence_reference


@pytest.mark.parametrize(
    "reference",
    (
        "https://service.invalid/callback?api_key=raw-api-key",
        "https://service.invalid/callback?api%5Fkey=raw-encoded-key",
        "https://service.invalid/callback?api%255Fkey=raw-twice-encoded-key",
        "https://service.invalid/callback?client_secret=raw-client-secret",
        "https://service.invalid/callback?password=raw-password",
        "https://service.invalid/callback?session_token=raw-session-token",
        "https://service.invalid/callback?secret=raw-secret",
        "https://service.invalid/callback?secret_key=raw-secret-key",
        "https://service.invalid/callback?private_key=raw-private-key",
        "https://service.invalid/callback?access_key=raw-access-key",
        "https://service.invalid/callback?id_token=raw-id-token",
        "https://service.invalid/callback?auth_token=raw-auth-token",
        "authorization=Basic raw-authorization",
        "X-API-Key: raw-header-key",
        "Proxy-Authorization: Basic raw-proxy-authorization",
        "Bearer+raw-form-token",
    ),
)
def test_public_evidence_hashes_common_credential_url_and_header_shapes(
    reference: str,
) -> None:
    presented = EvidenceReference(kind="test", reference=reference, label="Evidence")

    assert presented.reference.startswith("evidence-sha256:")
    assert reference not in presented.reference


@pytest.mark.parametrize(
    "reference",
    (
        "https://operator:raw-password@service.invalid/evidence",
        "https://operator%40team:raw-password@service.invalid/evidence",
        "https%3A%2F%2Foperator:raw-password%40service.invalid/evidence",
        "https://[invalid-host/evidence",
    ),
)
def test_public_evidence_hashes_url_userinfo_and_malformed_authority(
    reference: str,
) -> None:
    protected = safe_evidence_reference(reference)

    assert protected.startswith("evidence-sha256:")
    assert reference not in protected


def test_deeply_encoded_credential_marker_cannot_escape_public_boundary() -> None:
    reference = "credential://provider/project-1/writer"
    for _ in range(24):
        reference = quote(reference, safe="")

    assert len(reference.encode("utf-8")) <= 512
    protected = safe_evidence_reference(reference)

    assert protected.startswith("evidence-sha256:")
    assert reference not in protected


@pytest.mark.parametrize(
    "reference",
    (
        "https://service.invalid/report?status=healthy",
        "https://service.invalid/report?name=important%20report",
        "https://service.invalid/report?name=alpha+beta",
        "https://service.invalid/report?public_key=release-identity",
        "https://service.invalid/report?token_count=4",
        "health://project-1/service-api/healthy",
        "evidence://project-1/build/123",
    ),
)
def test_public_evidence_retains_nonsensitive_references(reference: str) -> None:
    assert safe_evidence_reference(reference) == reference


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "api-key",
        "apikey",
        "client_secret",
        "client-secret",
        "password",
        "passwd",
        "secret",
        "secret_key",
        "private_key",
        "access_key",
        "id_token",
        "session_token",
        "auth_token",
        "x-api-key",
        "authorization",
    ),
)
def test_public_evidence_hashes_all_common_credential_query_keys(key: str) -> None:
    reference = f"https://service.invalid/evidence?{key}=raw-credential"

    protected = safe_evidence_reference(reference)

    assert protected.startswith("evidence-sha256:")
    assert "raw-credential" not in protected
