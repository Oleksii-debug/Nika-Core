from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialState,
    IdentityRef,
    SecretRef,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
PROJECT_A = "product-a"
PROJECT_B = "product-b"
RAW_SECRET = "super-secret-test-value-never-serialize"


@dataclass(slots=True)
class FakeProtectedStore:
    _material: dict[tuple[str, int], str] = field(default_factory=dict)
    _handles: dict[str, tuple[str, int]] = field(default_factory=dict)
    _next_handle: int = 1

    def seed(self, secret_ref: str, generation: int, raw_secret: str) -> None:
        self._material[(secret_ref, generation)] = raw_secret

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self._material

    def issue_handle(
        self,
        *,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str:
        assert project_id
        assert audience
        assert scopes
        assert expires_at.tzinfo is not None
        if not self.contains(secret_ref, generation):
            raise AssertionError("fake store cannot issue a handle for missing material")
        handle = f"fake-protected-handle-{self._next_handle:08d}"
        self._next_handle += 1
        self._handles[handle] = (secret_ref, generation)
        return handle

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        for handle in [
            handle
            for handle, identity in self._handles.items()
            if identity == (secret_ref, generation)
        ]:
            del self._handles[handle]


def secret(
    secret_ref: str = "secret-a",
    *,
    project_id: str = PROJECT_A,
    provider: str = "github",
    generation: int = 1,
) -> SecretRef:
    return SecretRef(
        secret_ref,
        project_id,
        provider,
        "repository automation",
        frozenset({"repo:read", "checks:read"}),
        frozenset({"github-api", "build-node"}),
        generation,
    )


def broker_with_secret() -> tuple[CredentialBroker, FakeProtectedStore]:
    store = FakeProtectedStore()
    store.seed("secret-a", 1, RAW_SECRET)
    broker = CredentialBroker(store)
    broker.register_secret(secret(), now=NOW)
    return broker, store


def test_opaque_lease_and_audit_never_serialize_raw_secret() -> None:
    broker, _store = broker_with_secret()
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    evidence = broker.authorize_use(
        lease_id=lease.lease_id,
        project_id=PROJECT_A,
        scope="repo:read",
        now=NOW + timedelta(seconds=1),
    )

    serialized_surfaces = (
        repr(lease),
        repr(evidence),
        repr(broker.snapshot()),
        repr(broker.audit_events(PROJECT_A)),
    )
    assert all(RAW_SECRET not in surface for surface in serialized_surfaces)
    assert lease.handle_ref.startswith("fake-protected-handle-")
    assert evidence.secret_ref == "secret-a"


def test_project_cannot_use_or_enumerate_unrelated_secret() -> None:
    broker, _store = broker_with_secret()

    assert broker.list_project_secret_refs(PROJECT_B) == ()
    with pytest.raises(CredentialBrokerError, match="unavailable for project"):
        broker.issue_lease(
            project_id=PROJECT_B,
            secret_ref="secret-a",
            audience="github-api",
            scopes=frozenset({"repo:read"}),
            now=NOW,
        )


def test_scope_and_audience_are_attenuated_fail_closed() -> None:
    broker, _store = broker_with_secret()

    with pytest.raises(CredentialBrokerError, match="scopes exceed"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref="secret-a",
            audience="github-api",
            scopes=frozenset({"repo:write"}),
            now=NOW,
        )
    with pytest.raises(CredentialBrokerError, match="audience is not allowed"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref="secret-a",
            audience="unrelated-service",
            scopes=frozenset({"repo:read"}),
            now=NOW,
        )


def test_revocation_invalidates_existing_lease_and_future_use() -> None:
    broker, store = broker_with_secret()
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    assert lease.handle_ref in store._handles

    broker.revoke(project_id=PROJECT_A, secret_ref="secret-a", now=NOW + timedelta(seconds=1))

    assert lease.handle_ref not in store._handles
    assert broker.list_project_secret_refs(PROJECT_A)[0].state is CredentialState.REVOKED
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_A,
            scope="repo:read",
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(CredentialBrokerError, match="revoked"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref="secret-a",
            audience="github-api",
            scopes=frozenset({"repo:read"}),
            now=NOW + timedelta(seconds=2),
        )


def test_expired_lease_fails_closed() -> None:
    broker, _store = broker_with_secret()
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
        ttl_seconds=5,
    )

    with pytest.raises(CredentialBrokerError, match="expired"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_A,
            scope="repo:read",
            now=NOW + timedelta(seconds=5),
        )


def test_rotation_requires_preseeded_generation_and_revokes_old_lease() -> None:
    broker, store = broker_with_secret()
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )

    with pytest.raises(CredentialBrokerError, match="next secret generation"):
        broker.rotate(project_id=PROJECT_A, secret_ref="secret-a", now=NOW)

    store.seed("secret-a", 2, "rotated-secret-never-serialize")
    rotated = broker.rotate(project_id=PROJECT_A, secret_ref="secret-a", now=NOW)

    assert rotated.generation == 2
    assert lease.handle_ref not in store._handles
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_A,
            scope="repo:read",
            now=NOW + timedelta(seconds=1),
        )


def test_identity_binding_cannot_cross_project_boundary() -> None:
    store = FakeProtectedStore()
    store.seed("secret-a", 1, RAW_SECRET)
    store.seed("secret-b", 1, "another-secret")
    broker = CredentialBroker(store)
    broker.register_secret(secret(), now=NOW)
    broker.register_secret(secret("secret-b", project_id=PROJECT_B), now=NOW)

    with pytest.raises(CredentialBrokerError, match="another project"):
        broker.register_identity(
            IdentityRef(
                "identity-a",
                PROJECT_A,
                "github",
                "subject-ref-a",
                ("secret-a", "secret-b"),
            )
        )


def test_identity_is_only_visible_to_own_project() -> None:
    broker, _store = broker_with_secret()
    identity = IdentityRef(
        "identity-a",
        PROJECT_A,
        "github",
        "subject-ref-a",
        ("secret-a",),
    )
    broker.register_identity(identity)

    assert broker.get_identity(project_id=PROJECT_A, identity_ref="identity-a") == identity
    with pytest.raises(CredentialBrokerError, match="unavailable for project"):
        broker.get_identity(project_id=PROJECT_B, identity_ref="identity-a")


def test_restart_snapshot_never_restores_active_leases() -> None:
    broker, store = broker_with_secret()
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    snapshot = broker.snapshot()
    restored = CredentialBroker(store)
    restored.restore(snapshot)

    assert RAW_SECRET not in repr(snapshot)
    assert restored.list_project_secret_refs(PROJECT_A) == broker.list_project_secret_refs(PROJECT_A)
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        restored.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_A,
            scope="repo:read",
            now=NOW + timedelta(seconds=1),
        )


def test_snapshot_rejects_cross_project_identity_tampering() -> None:
    broker, store = broker_with_secret()
    snapshot = broker.snapshot()
    tampered = type(snapshot)(
        snapshot.secrets,
        (
            IdentityRef(
                "identity-tampered",
                PROJECT_B,
                "github",
                "subject-ref-b",
                ("secret-a",),
            ),
        ),
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError, match="crosses project boundary"):
        CredentialBroker(store).restore(tampered)


def test_registration_requires_material_already_in_protected_store() -> None:
    broker = CredentialBroker(FakeProtectedStore())

    with pytest.raises(CredentialBrokerError, match="does not contain"):
        broker.register_secret(secret(), now=NOW)


def test_audit_is_project_scoped_and_contains_only_reference_metadata() -> None:
    broker, store = broker_with_secret()
    store.seed("secret-b", 1, "other-project-secret")
    broker.register_secret(secret("secret-b", project_id=PROJECT_B), now=NOW)

    events_a = broker.audit_events(PROJECT_A)
    events_b = broker.audit_events(PROJECT_B)

    assert {event.project_id for event in events_a} == {PROJECT_A}
    assert {event.project_id for event in events_b} == {PROJECT_B}
    assert RAW_SECRET not in repr(events_a)
