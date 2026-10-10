from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlsplit

_MAX_EVIDENCE_REFERENCE = 512
_SENSITIVE_REFERENCE_MARKERS = (
    "credential://",
    "credential-use:",
    "approval://",
    "secret://",
    "protected-handle:",
    "protected_handle:",
    "provider-session:",
    "provider_session:",
    "authorization:",
    "bearer ",
    "access_token",
    "refresh_token",
    "token=",
    "api_key=",
    "api-key=",
    "apikey=",
    "client_secret=",
    "client-secret=",
    "password=",
    "passwd=",
    "secret=",
    "secret_key=",
    "private_key=",
    "access_key=",
    "id_token=",
    "session_token=",
    "auth_token=",
    "x-api-key:",
    "x-api-key=",
    "proxy-authorization:",
    "authorization=",
)


def safe_evidence_reference(reference: str) -> str:
    """Return a bounded user-facing evidence reference without credential material.

    Product Factory evidence is intentionally opaque and may include credential-use
    audit identities or provider-owned references. PF5 preserves ordinary evidence
    references verbatim, but one-way hashes anything that is sensitive by shape or
    too large for the public EvidenceReference contract.
    """

    # Encoded URL keys and userinfo cannot bypass the public evidence boundary.
    normalized = reference.strip().casefold()
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    sensitive = unquote(normalized) != normalized or any(
        marker in normalized for marker in _SENSITIVE_REFERENCE_MARKERS
    )
    if not sensitive and "://" in normalized:
        try:
            # Userinfo may contain a password even without a named token parameter.
            sensitive = urlsplit(normalized).username is not None
        except ValueError:
            # Malformed authority must not bypass the public evidence boundary.
            sensitive = True
    if sensitive or len(reference) > _MAX_EVIDENCE_REFERENCE:
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return f"evidence-sha256:{digest}"
    return reference
