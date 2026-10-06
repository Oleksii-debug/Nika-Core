from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import (
    ProductCommandCenter,
    ProductCommandCenterScopeError,
)
from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_credentials import (
    CredentialAuditEvent,
    CredentialBrokerSnapshot,
    CredentialState,
    IdentityRef,
    SecretRef,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
SECRET_REF = "credential://github/project-writer"


def _center(tmp_path) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="p1",
        name="Credential project",
        spec=ProductProjectSpec(
            goal="Build product",
            desired_outcome="Durable product",
            credential_refs=(SECRET_REF,),
        ),
        idempotency_key="create:p1",
    )
    return ProductCommandCenter(service)


def _secret(
    *,
    project_id: str = "p1",
    provider: str = "github",
    state: CredentialState = CredentialState.ACTIVE,
    secret_ref: str = SECRET_REF,
) -> SecretRef:
    return SecretRef(
        secret_ref,
        project_id,
        provider,
        "repository writer",
        frozenset({"contents:write"}),
        frozenset({"github.com"}),
        state=state,
    )


def _snapshot(
    *,
    secrets=(),
    identities=(),
    audits=(),
) -> CredentialBrokerSnapshot:
    return CredentialBrokerSnapshot(
        tuple(secrets),
        tuple(identities),
        tuple(audits),
        1,
        1,
    )


def test_declared_credential_is_presented_without_opaque_ref_or_raw_audit_detail(
    tmp_path,
) -> None:
    center = _center(tmp_path)
    event = CredentialAuditEvent(
        "event-sensitive-name",
        "registered",
        "p1",
        SECRET_REF,
        NOW,
        "internal broker detail must not be rendered",
    )

    detail = center.inspect_project(
        "p1",
        credentials=_snapshot(secrets=(_secret(),), audits=(event,)),
    )
    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )
    serialized = detail.model_dump_json()

    assert credential.state == "active"
    assert credential.evidence[0].reference.startswith("credential-audit-sha256:")
    assert SECRET_REF not in serialized
    assert "event-sensitive-name" not in serialized
    assert "internal broker detail" not in serialized
    assert "credential://" not in serialized


def test_missing_declared_credential_becomes_explicit_blocker(tmp_path) -> None:
    center = _center(tmp_path)

    detail = center.inspect_project("p1", credentials=_snapshot())
    blockers = [
        item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER
    ]

    assert len(blockers) == 1
    assert "unavailable" in blockers[0].label.lower()
    assert detail.summary.blocker_count == 1
    assert SECRET_REF not in detail.model_dump_json()


def test_revoked_declared_credential_is_visible_and_blocks_work(tmp_path) -> None:
    center = _center(tmp_path)

    detail = center.inspect_project(
        "p1",
        credentials=_snapshot(
            secrets=(_secret(state=CredentialState.REVOKED),),
        ),
    )

    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )
    blocker = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER
    )
    assert credential.state == "revoked"
    assert "revoked" in blocker.detail.lower()
    assert detail.summary.blocker_count == 1


def test_broker_only_project_credential_is_not_falsely_marked_declared(tmp_path) -> None:
    center = _center(tmp_path)
    extra = _secret(secret_ref="credential://github/broker-only")

    detail = center.inspect_project(
        "p1",
        credentials=_snapshot(secrets=(_secret(), extra)),
    )
    credentials = [
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    ]

    assert {item.state for item in credentials} == {"active", "active_unlinked"}
    assert "credential://github/broker-only" not in detail.model_dump_json()


def test_declared_ref_resolving_to_foreign_project_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)

    with pytest.raises(ProductCommandCenterScopeError, match="another project"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(_secret(project_id="p2"),)),
        )


def test_foreign_identity_touching_target_secret_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    identity = IdentityRef(
        "identity-foreign",
        "p2",
        "github",
        "subject://foreign",
        (SECRET_REF,),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="identity crosses"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(_secret(),), identities=(identity,)),
        )


def test_identity_provider_mismatch_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    identity = IdentityRef(
        "identity-target",
        "p1",
        "gitlab",
        "subject://target",
        (SECRET_REF,),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="provider does not match"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(_secret(),), identities=(identity,)),
        )


def test_cross_project_audit_binding_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    event = CredentialAuditEvent(
        "event-1",
        "used",
        "p2",
        SECRET_REF,
        NOW,
        "foreign audit",
    )

    with pytest.raises(ProductCommandCenterScopeError, match="audit event crosses"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(_secret(),), audits=(event,)),
        )


def test_duplicate_broker_identity_keys_fail_closed(tmp_path) -> None:
    center = _center(tmp_path)
    secret = _secret()
    event = CredentialAuditEvent("event-1", "used", "p1", SECRET_REF, NOW, "detail")

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate secret-reference"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(secret, secret)),
        )
    with pytest.raises(ProductCommandCenterScopeError, match="duplicate audit-event"):
        center.inspect_project(
            "p1",
            credentials=_snapshot(secrets=(secret,), audits=(event, event)),
        )


def test_credential_metadata_and_audit_history_are_bounded(tmp_path) -> None:
    center = _center(tmp_path)
    huge_secret = SecretRef(
        SECRET_REF,
        "p1",
        "provider-" + "p" * 500,
        "purpose-" + "q" * 1200,
        frozenset({f"scope-{index}-" + "s" * 300 for index in range(20)}),
        frozenset({f"audience-{index}-" + "a" * 300 for index in range(20)}),
    )
    audits = tuple(
        CredentialAuditEvent(
            f"event-{index}-" + "e" * 700,
            "action-" + "x" * 400,
            "p1",
            SECRET_REF,
            NOW,
            "detail-not-rendered",
        )
        for index in range(25)
    )

    detail = center.inspect_project(
        "p1",
        credentials=_snapshot(secrets=(huge_secret,), audits=audits),
    )
    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )

    assert len(credential.label) <= 240
    assert len(credential.detail) <= 4000
    assert len(credential.evidence) == 20
    assert all(
        item.reference.startswith("credential-audit-sha256:")
        for item in credential.evidence
    )
    assert SECRET_REF not in detail.model_dump_json()
