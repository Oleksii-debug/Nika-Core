from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.credential_adapter import credential_status_entries
from nika_core.product_factory_credentials import (
    CredentialAuditEvent,
    CredentialBrokerSnapshot,
    SecretRef,
)

NOW = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
SECRET_REF = "credential://project/opaque"


def test_credential_presentation_bounds_untrusted_metadata_without_exposing_reference() -> None:
    provider = "p" * 500
    purpose = "purpose-" + "x" * 1000
    scopes = frozenset({f"scope-{index}-" + "s" * 500 for index in range(20)})
    audiences = frozenset({f"audience-{index}-" + "a" * 500 for index in range(20)})
    secret = SecretRef(
        SECRET_REF,
        "project-1",
        provider,
        purpose,
        scopes,
        audiences,
    )
    snapshot = CredentialBrokerSnapshot((secret,), (), (), 1, 1)

    entries = credential_status_entries("project-1", (SECRET_REF,), snapshot)
    credential = next(item for item in entries if item.kind is ProductStatusKind.CREDENTIAL)
    serialized = "\n".join(item.model_dump_json() for item in entries)

    assert len(credential.label) <= 240
    assert len(credential.detail) <= 4000
    assert "ProductProject link: declared" in credential.detail
    assert SECRET_REF not in serialized
    assert "credential://" not in serialized


def test_oversized_audit_identity_is_hashed_and_action_label_is_bounded() -> None:
    secret = SecretRef(
        SECRET_REF,
        "project-1",
        "github",
        "writer",
        frozenset({"contents:write"}),
        frozenset({"github.com"}),
    )
    oversized_event_id = "event-" + "e" * 2000
    event = CredentialAuditEvent(
        oversized_event_id,
        "action-" + "a" * 1000,
        "project-1",
        SECRET_REF,
        NOW,
        "detail",
    )
    snapshot = CredentialBrokerSnapshot((secret,), (), (event,), 1, 1)

    entries = credential_status_entries("project-1", (SECRET_REF,), snapshot)
    credential = next(item for item in entries if item.kind is ProductStatusKind.CREDENTIAL)
    evidence = credential.evidence[0]

    assert evidence.reference.startswith("credential-audit-sha256:")
    assert len(evidence.reference) <= 512
    assert len(evidence.label) <= 240
    assert oversized_event_id not in evidence.reference


def test_missing_credential_blocker_uses_only_one_way_reference_digest() -> None:
    snapshot = CredentialBrokerSnapshot((), (), (), 1, 1)

    entries = credential_status_entries("project-1", (SECRET_REF,), snapshot)
    blocker = next(item for item in entries if item.kind is ProductStatusKind.BLOCKER)

    assert blocker.item_id.startswith("credential:")
    assert SECRET_REF not in blocker.item_id
    assert SECRET_REF not in blocker.model_dump_json()
