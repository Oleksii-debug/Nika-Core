from __future__ import annotations

import hashlib
from urllib.parse import unquote, unquote_plus, urlsplit

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


def _strict_utf8(reference: str) -> bytes:
    try:
        return str.encode(reference, "utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError) as exc:
        raise ValueError("evidence reference must be valid UTF-8 text") from exc


def _contains_sensitive_marker(reference: str) -> bool:
    return any(marker in reference for marker in _SENSITIVE_REFERENCE_MARKERS)


def _is_sensitive(reference: str) -> bool:
    normalized = reference.strip().casefold()
    for _ in range(_MAX_EVIDENCE_REFERENCE_BYTES):
        if _contains_sensitive_marker(normalized):
            return True

        form_decoded = unquote_plus(normalized).casefold()
        if _contains_sensitive_marker(form_decoded):
            return True

        if "://" in normalized or normalized.startswith("//"):
            try:
                if urlsplit(normalized).username is not None:
                    return True
            except ValueError:
                # Malformed URI authority must not bypass the public evidence boundary.
                return True

        decoded = unquote(normalized).casefold()
        if decoded == normalized:
            return False
        normalized = decoded

    # A <=512-byte reference cannot require unbounded decoding legitimately.
    return True


def safe_evidence_reference(reference: str) -> str:
    """Return a bounded user-facing evidence reference without credential material.

    Product Factory evidence is intentionally opaque and may include credential-use
    audit identities or provider-owned references. PF5 preserves ordinary valid
    Unicode references verbatim, but one-way hashes anything that is sensitive by
    shape or exceeds the public evidence byte budget.
    """

    encoded = _strict_utf8(reference)
    canonical = encoded.decode("utf-8", errors="strict")
    if len(encoded) > _MAX_EVIDENCE_REFERENCE_BYTES:
        digest = hashlib.sha256(encoded).hexdigest()
        return f"evidence-sha256:{digest}"

    if _is_sensitive(canonical):
        digest = hashlib.sha256(encoded).hexdigest()
        return f"evidence-sha256:{digest}"
    return canonical
