from __future__ import annotations

import hashlib
from urllib.parse import unquote_plus

_MAX_EVIDENCE_REFERENCE_BYTES = 512
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
)


def _strict_utf8(reference: str) -> bytes:
    try:
        return reference.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("evidence reference must be valid UTF-8 text") from exc


def _is_sensitive(reference: str) -> bool:
    normalized = reference.strip().casefold()
    for _ in range(_MAX_EVIDENCE_REFERENCE_BYTES):
        if any(marker in normalized for marker in _SENSITIVE_REFERENCE_MARKERS):
            return True
        decoded = unquote_plus(normalized).casefold()
        if decoded == normalized:
            return False
        normalized = decoded
    return True


def safe_evidence_reference(reference: str) -> str:
    """Return a bounded user-facing evidence reference without credential material.

    Product Factory evidence is intentionally opaque and may include credential-use
    audit identities or provider-owned references. PF5 preserves ordinary valid
    Unicode references verbatim, but one-way hashes anything that is sensitive by
    shape or exceeds the public evidence byte budget.
    """

    encoded = _strict_utf8(reference)
    if len(encoded) > _MAX_EVIDENCE_REFERENCE_BYTES:
        digest = hashlib.sha256(encoded).hexdigest()
        return f"evidence-sha256:{digest}"

    if _is_sensitive(reference):
        digest = hashlib.sha256(encoded).hexdigest()
        return f"evidence-sha256:{digest}"
    return reference
