from __future__ import annotations

import hashlib

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
)


def safe_evidence_reference(reference: str) -> str:
    """Return a bounded user-facing evidence reference without credential material.

    Product Factory evidence is intentionally opaque and may include credential-use
    audit identities or provider-owned references. PF5 preserves ordinary evidence
    references verbatim, but one-way hashes anything that is sensitive by shape or
    too large for the public EvidenceReference contract.
    """

    normalized = reference.strip().casefold()
    sensitive = any(marker in normalized for marker in _SENSITIVE_REFERENCE_MARKERS)
    if sensitive or len(reference) > _MAX_EVIDENCE_REFERENCE:
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return f"evidence-sha256:{digest}"
    return reference
