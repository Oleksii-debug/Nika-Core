from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlsplit

_MAX_EVIDENCE_REFERENCE = 512
_MAX_DECODE_PASSES = 4
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
    "authorization=",
    "cookie:",
    "cookie=",
    "bearer ",
    "access_token",
    "refresh_token",
    "auth_token",
    "api_key",
    "api-key",
    "client_secret",
    "client-secret",
    "private_key",
    "private-key",
    "password=",
    "password:",
    "passwd=",
    "secret=",
    "token=",
)


def _normalized_reference_views(reference: str) -> tuple[str, ...]:
    """Return bounded decoded views so nested URL encoding cannot hide credentials."""
    views: list[str] = []
    current = reference
    for _ in range(_MAX_DECODE_PASSES):
        normalized = current.strip().casefold()
        if normalized not in views:
            views.append(normalized)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return tuple(views)


def _reference_is_sensitive(reference: str) -> bool:
    for normalized in _normalized_reference_views(reference):
        if any(marker in normalized for marker in _SENSITIVE_REFERENCE_MARKERS):
            return True
        parsed = urlsplit(normalized)
        if parsed.scheme and parsed.netloc and (
            parsed.username is not None or parsed.password is not None
        ):
            return True
    return False


def safe_evidence_reference(reference: str) -> str:
    """Return a bounded user-facing evidence reference without credential material.

    Product Factory evidence is intentionally opaque and may include credential-use
    audit identities or provider-owned references. PF5 preserves ordinary evidence
    references verbatim, but one-way hashes anything that is sensitive by shape or
    too large for the public EvidenceReference contract.
    """

    sensitive = _reference_is_sensitive(reference)
    if sensitive or len(reference) > _MAX_EVIDENCE_REFERENCE:
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return f"evidence-sha256:{digest}"
    return reference
