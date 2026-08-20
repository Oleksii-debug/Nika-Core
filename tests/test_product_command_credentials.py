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

NOW = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
TARGET_REF = "credential://github/project-1/writer"
FOREIGN_REF = "credential://github/project-2/writer"
BROKER_ONLY_REF = "credential://github/project-1/unused"


def _center(tmp_path, credential_refs: tuple[str, ...] = (TARGET_REF,)) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="project-1",
        name="Expense",
        spec=ProductProjectSpec(
            goal="Build accessible expense app",
            desired_outcome="Packaged accessible Windows application",
            credential_refs=credential_refs,
        ),
        idempotency_key="create:project-1",
    )
    return ProductCommandCenter(service)


def _secret(
    secret_ref: str,
    *,
    project_id: str = "project-1",
    provider: str = "github",
    purpose: str = "repository writer",
    state: CredentialState = CredentialState.ACTIVE,
) -> SecretRef:
    return SecretRef(
        secret_ref,
        project_id,
        provider,
        purpose,
        frozenset({"contents:write", "pull_requests:write"}),
        frozenset({"github.com"}),
        2,
        state,
    )


def _audit(
    event_id: str,
    secret_ref: str = TARGET_REF,
    *,
    project_id: str = "project-1",
    action: str = "use",
) -> CredentialAuditEvent:
    return CredentialAuditEvent(
        event_id,
        action,
        project_id,
        secret_ref,
        NOW,
        "audience=github.com;scope=contents:write",
    )


def _snapshot(
    *,
    secrets: tuple[SecretRef, ...] = (),
    identities: tuple[IdentityRef, ...] = (),
    audit_events: tuple[CredentialAuditEvent, ...] = (),
) -> CredentialBrokerSnapshot:
    return CredentialBrokerSnapshot(secrets, identities, audit_events, 1, 1)


def test_active_declared_credential_is_visible_without_reference_disclosure(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF),),
        audit_events=(_audit("credential-event-00000001", action="register"),),
    )

    detail = center.inspect_project("project-1", credentials=snapshot)
    serialized = detail.model_dump_json()
    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )

    assert credential.state == "active"
    assert credential.label == "Credential github / repository writer"
    assert "generation: 2" in credential.detail
    assert "contents:write" in credential.detail
    assert "github.com" in credential.detail
    assert credential.evidence[0].reference == "credential-event-00000001"
    assert detail.summary.blocker_count == 0
    assert TARGET_REF not in serialized
    assert "credential://" not in serialized


def test_missing_declared_credential_creates_redacted_blocker(tmp_path) -> None:
    center = _center(tmp_path)

    detail = center.inspect_project("project-1", credentials=_snapshot())
    serialized = detail.model_dump_json()

    blocker = next(item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER)
    assert blocker.state == "active"
    assert "not available" in blocker.detail
    assert detail.summary.blocker_count == 1
    assert TARGET_REF not in serialized
    assert "credential://" not in serialized


def test_revoked_declared_credential_creates_explicit_blocker(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF, state=CredentialState.REVOKED),),
        audit_events=(_audit("credential-event-00000002", action="revoke"),),
    )

    detail = center.inspect_project("project-1", credentials=snapshot)

    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )
    blocker = next(item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER)
    assert credential.state == "revoked"
    assert "re-authorization" in blocker.detail.lower()
    assert blocker.evidence[0].reference == "credential-event-00000002"
    assert detail.summary.blocker_count == 1


def test_broker_only_project_credential_is_inspectable_but_not_declared(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=())
    snapshot = _snapshot(secrets=(_secret(BROKER_ONLY_REF, purpose="unused connector"),))

    detail = center.inspect_project("project-1", credentials=snapshot)
    serialized = detail.model_dump_json()

    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )
    assert credential.state == "active_unlinked"
    assert "ProductProject link: broker-only" in credential.detail
    assert detail.summary.blocker_count == 0
    assert BROKER_ONLY_REF not in serialized


def test_foreign_project_credential_is_not_presented(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=())
    snapshot = _snapshot(
        secrets=(
            _secret(BROKER_ONLY_REF, purpose="target"),
            _secret(FOREIGN_REF, project_id="project-2", purpose="foreign"),
        )
    )

    detail = center.inspect_project("project-1", credentials=snapshot)
    serialized = detail.model_dump_json()

    assert "target" in serialized
    assert "foreign" not in serialized
    assert "project-2" not in serialized
    assert FOREIGN_REF not in serialized


def test_declared_reference_resolving_to_foreign_project_fails_closed(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=(FOREIGN_REF,))
    snapshot = _snapshot(secrets=(_secret(FOREIGN_REF, project_id="project-2"),))

    with pytest.raises(ProductCommandCenterScopeError, match="another project"):
        center.inspect_project("project-1", credentials=snapshot)


def test_target_identity_cannot_bind_foreign_secret(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=())
    snapshot = _snapshot(
        secrets=(_secret(FOREIGN_REF, project_id="project-2"),),
        identities=(
            IdentityRef(
                "identity-target",
                "project-1",
                "github",
                "subject://target",
                (FOREIGN_REF,),
            ),
        ),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="outside ProductProject scope"):
        center.inspect_project("project-1", credentials=snapshot)


def test_foreign_identity_cannot_bind_target_secret(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF),),
        identities=(
            IdentityRef(
                "identity-foreign",
                "project-2",
                "github",
                "subject://foreign",
                (TARGET_REF,),
            ),
        ),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="crosses ProductProject boundary"):
        center.inspect_project("project-1", credentials=snapshot)


def test_identity_provider_mismatch_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF, provider="github"),),
        identities=(
            IdentityRef(
                "identity-target",
                "project-1",
                "azure",
                "subject://target",
                (TARGET_REF,),
            ),
        ),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="provider does not match"):
        center.inspect_project("project-1", credentials=snapshot)


def test_target_audit_event_cannot_reference_foreign_secret(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=())
    snapshot = _snapshot(
        secrets=(_secret(FOREIGN_REF, project_id="project-2"),),
        audit_events=(_audit("event-cross", FOREIGN_REF, project_id="project-1"),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="audit event crosses"):
        center.inspect_project("project-1", credentials=snapshot)


def test_foreign_audit_event_cannot_reference_target_secret(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF),),
        audit_events=(_audit("event-cross", TARGET_REF, project_id="project-2"),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="audit event crosses"):
        center.inspect_project("project-1", credentials=snapshot)


def test_duplicate_secret_reference_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    snapshot = _snapshot(secrets=(_secret(TARGET_REF), _secret(TARGET_REF)))

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate secret-reference"):
        center.inspect_project("project-1", credentials=snapshot)


def test_duplicate_identity_reference_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    first = IdentityRef(
        "identity-target",
        "project-1",
        "github",
        "subject://target",
        (TARGET_REF,),
    )
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF),),
        identities=(first, first),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate identity-reference"):
        center.inspect_project("project-1", credentials=snapshot)


def test_duplicate_audit_event_identity_fails_closed(tmp_path) -> None:
    center = _center(tmp_path)
    event = _audit("event-duplicate")
    snapshot = _snapshot(
        secrets=(_secret(TARGET_REF),),
        audit_events=(event, event),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate audit-event"):
        center.inspect_project("project-1", credentials=snapshot)


def test_duplicate_declared_project_credential_refs_fail_closed(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=(TARGET_REF, TARGET_REF))

    with pytest.raises(ProductCommandCenterScopeError, match="duplicate credential references"):
        center.inspect_project(
            "project-1",
            credentials=_snapshot(secrets=(_secret(TARGET_REF),)),
        )


def test_empty_declared_project_credential_ref_fails_closed(tmp_path) -> None:
    center = _center(tmp_path, credential_refs=("",))

    with pytest.raises(ProductCommandCenterScopeError, match="empty credential reference"):
        center.inspect_project("project-1", credentials=_snapshot())


def test_credential_audit_evidence_is_bounded_to_latest_twenty_events(tmp_path) -> None:
    center = _center(tmp_path)
    events = tuple(_audit(f"event-{index:02d}") for index in range(25))
    snapshot = _snapshot(secrets=(_secret(TARGET_REF),), audit_events=events)

    detail = center.inspect_project("project-1", credentials=snapshot)
    credential = next(
        item for item in detail.statuses if item.kind is ProductStatusKind.CREDENTIAL
    )

    assert len(credential.evidence) == 20
    assert credential.evidence[0].reference == "event-05"
    assert credential.evidence[-1].reference == "event-24"


def test_command_service_context_reads_visible_detail_and_opaque_refs_together(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="project-1",
        name="Expense",
        spec=ProductProjectSpec(
            goal="Build accessible expense app",
            desired_outcome="Packaged app",
            credential_refs=(TARGET_REF,),
        ),
        idempotency_key="create:project-1",
    )

    detail, refs = service.inspect_project_context("project-1")

    assert refs == (TARGET_REF,)
    assert TARGET_REF not in detail.model_dump_json()
    assert "credential://" not in detail.model_dump_json()
