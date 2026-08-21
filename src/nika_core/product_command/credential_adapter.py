from __future__ import annotations

import hashlib
from collections import defaultdict

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_factory_credentials import (
    CredentialAuditEvent,
    CredentialBrokerSnapshot,
    CredentialState,
    SecretRef,
)

_MAX_AUDIT_EVIDENCE_PER_CREDENTIAL = 20
_MAX_STATUS_LABEL = 240
_MAX_STATUS_DETAIL = 4000
_MAX_EVIDENCE_REFERENCE = 512
_MAX_EVIDENCE_LABEL = 240


def credential_status_entries(
    project_id: str,
    declared_refs: tuple[str, ...],
    snapshot: CredentialBrokerSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    """Project credential state without exposing opaque secret-reference strings."""
    secrets = {
        secret.secret_ref: secret
        for secret in snapshot.secrets
        if secret.project_id == project_id
    }
    audits: dict[str, list[CredentialAuditEvent]] = defaultdict(list)
    for event in snapshot.audit_events:
        if event.project_id == project_id and event.secret_ref in secrets:
            audits[event.secret_ref].append(event)

    declared = set(declared_refs)
    all_refs = sorted(declared | set(secrets))
    entries: list[ProductStatusEntry] = []
    for secret_ref in all_refs:
        secret = secrets.get(secret_ref)
        opaque_id = _opaque_reference_id(secret_ref)
        if secret is None:
            entries.append(
                ProductStatusEntry(
                    kind=ProductStatusKind.BLOCKER,
                    item_id=f"credential:{opaque_id}:missing",
                    label="Credential binding unavailable",
                    state="active",
                    detail=(
                        "ProductProject declares an opaque credential reference that is not "
                        "available in the project-scoped Credential Broker snapshot."
                    ),
                )
            )
            continue

        linked = secret_ref in declared
        entries.append(
            _credential_entry(
                secret,
                opaque_id=opaque_id,
                linked=linked,
                audit_events=tuple(audits.get(secret_ref, ())),
            )
        )
        if linked and secret.state is CredentialState.REVOKED:
            entries.append(
                ProductStatusEntry(
                    kind=ProductStatusKind.BLOCKER,
                    item_id=f"credential:{opaque_id}:revoked",
                    label=_bounded_text(
                        f"Credential blocker: {secret.provider} / {secret.purpose}",
                        _MAX_STATUS_LABEL,
                    ),
                    state="active",
                    detail=(
                        "A credential declared by ProductProject is revoked. Re-authorization "
                        "or an approved replacement is required before dependent work resumes."
                    ),
                    evidence=_audit_evidence(tuple(audits.get(secret_ref, ()))),
                )
            )
    return tuple(entries)


def _credential_entry(
    secret: SecretRef,
    *,
    opaque_id: str,
    linked: bool,
    audit_events: tuple[CredentialAuditEvent, ...],
) -> ProductStatusEntry:
    scopes = ", ".join(sorted(secret.scopes))
    audiences = ", ".join(sorted(secret.allowed_audiences))
    link_state = "declared" if linked else "broker-only"
    visible_state = secret.state.value if linked else f"{secret.state.value}_unlinked"
    label = _bounded_text(
        f"Credential {secret.provider} / {secret.purpose}",
        _MAX_STATUS_LABEL,
    )
    detail = _bounded_text(
        f"Provider: {secret.provider}; purpose: {secret.purpose}; generation: "
        f"{secret.generation}; ProductProject link: {link_state}; scopes: {scopes}; "
        f"audiences: {audiences}.",
        _MAX_STATUS_DETAIL,
    )
    return ProductStatusEntry(
        kind=ProductStatusKind.CREDENTIAL,
        item_id=f"credential:{opaque_id}",
        label=label,
        state=visible_state,
        detail=detail,
        evidence=_audit_evidence(audit_events),
    )


def _audit_evidence(
    events: tuple[CredentialAuditEvent, ...],
) -> tuple[EvidenceReference, ...]:
    selected = events[-_MAX_AUDIT_EVIDENCE_PER_CREDENTIAL:]
    return tuple(
        EvidenceReference(
            kind="credential_audit",
            reference=_evidence_reference(event.event_id),
            label=_bounded_text(
                f"Credential broker audit: {event.action}",
                _MAX_EVIDENCE_LABEL,
            ),
        )
        for event in selected
    )


def _evidence_reference(event_id: str) -> str:
    if len(event_id) <= _MAX_EVIDENCE_REFERENCE:
        return event_id
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"credential-audit-sha256:{digest}"


def _opaque_reference_id(secret_ref: str) -> str:
    return hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"
