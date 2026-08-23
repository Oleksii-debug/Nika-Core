from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    SecretRef,
)
from nika_core.product_factory_windows_credentials import (
    ProtectedCredentialStoreError,
    WindowsCredentialStore,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
RAW_SECRET = "integration-secret-never-serialize"
PROJECT = "product-a"


@dataclass(slots=True)
class FakePersistentWinVault:
    passwords: dict[tuple[str, str], str] = field(default_factory=dict)
    persist: object = "local machine"

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.passwords[(service, username)]


def secret(generation: int = 1) -> SecretRef:
    return SecretRef(
        "secret-a",
        PROJECT,
        "github",
        "repository automation",
        frozenset({"repo:read", "checks:read"}),
        frozenset({"github-api"}),
        generation,
    )


def test_windows_store_satisfies_broker_handle_and_revocation_contract() -> None:
    backend = FakePersistentWinVault()
    store = WindowsCredentialStore(backend)
    store.provision_secret("secret-a", 1, RAW_SECRET)
    broker = CredentialBroker(store)
    broker.register_secret(secret(), now=NOW)

    lease = broker.issue_lease(
        project_id=PROJECT,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    receipt = store.validate_handle(
        handle_ref=lease.handle_ref,
        project_id=PROJECT,
        audience="github-api",
        scope="repo:read",
        now=NOW + timedelta(seconds=1),
    )
    evidence = broker.authorize_use(
        lease_id=lease.lease_id,
        project_id=PROJECT,
        scope="repo:read",
        now=NOW + timedelta(seconds=1),
    )

    assert receipt.secret_ref == evidence.secret_ref == "secret-a"
    assert RAW_SECRET not in repr(store)
    assert RAW_SECRET not in repr(lease)
    assert RAW_SECRET not in repr(evidence)

    broker.revoke(project_id=PROJECT, secret_ref="secret-a", now=NOW + timedelta(seconds=2))

    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=lease.handle_ref,
            project_id=PROJECT,
            audience="github-api",
            scope="repo:read",
            now=NOW + timedelta(seconds=3),
        )
    assert store.delete_secret("secret-a", 1) is True


def test_broker_rotation_uses_preprovisioned_windows_generation_and_invalidates_old_handle(
) -> None:
    backend = FakePersistentWinVault()
    store = WindowsCredentialStore(backend)
    store.provision_secret("secret-a", 1, RAW_SECRET)
    store.provision_secret("secret-a", 2, "rotated-integration-secret")
    broker = CredentialBroker(store)
    broker.register_secret(secret(), now=NOW)
    old_lease = broker.issue_lease(
        project_id=PROJECT,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )

    rotated = broker.rotate(
        project_id=PROJECT,
        secret_ref="secret-a",
        now=NOW + timedelta(seconds=1),
    )
    new_lease = broker.issue_lease(
        project_id=PROJECT,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW + timedelta(seconds=2),
    )

    assert rotated.generation == 2
    assert new_lease.generation == 2
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=old_lease.handle_ref,
            project_id=PROJECT,
            audience="github-api",
            scope="repo:read",
            now=NOW + timedelta(seconds=2),
        )
    assert store.validate_handle(
        handle_ref=new_lease.handle_ref,
        project_id=PROJECT,
        audience="github-api",
        scope="repo:read",
        now=NOW + timedelta(seconds=3),
    ).generation == 2

    assert store.delete_secret("secret-a", 1) is True
    assert store.delete_secret("secret-a", 2) is True


def test_broker_and_windows_store_restart_preserve_reference_not_lease() -> None:
    backend = FakePersistentWinVault()
    first_store = WindowsCredentialStore(backend)
    first_store.provision_secret("secret-a", 1, RAW_SECRET)
    first_broker = CredentialBroker(first_store)
    first_broker.register_secret(secret(), now=NOW)
    old_lease = first_broker.issue_lease(
        project_id=PROJECT,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    snapshot = first_broker.snapshot()

    restarted_store = WindowsCredentialStore(backend)
    restarted_broker = CredentialBroker(restarted_store)
    restarted_broker.restore(snapshot)

    assert restarted_store.contains("secret-a", 1)
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        restarted_broker.authorize_use(
            lease_id=old_lease.lease_id,
            project_id=PROJECT,
            scope="repo:read",
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        restarted_store.validate_handle(
            handle_ref=old_lease.handle_ref,
            project_id=PROJECT,
            audience="github-api",
            scope="repo:read",
            now=NOW + timedelta(seconds=1),
        )

    fresh_lease = restarted_broker.issue_lease(
        project_id=PROJECT,
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW + timedelta(seconds=2),
    )
    assert restarted_store.validate_handle(
        handle_ref=fresh_lease.handle_ref,
        project_id=PROJECT,
        audience="github-api",
        scope="repo:read",
        now=NOW + timedelta(seconds=3),
    ).generation == 1
    assert RAW_SECRET not in repr(snapshot)
    assert restarted_store.delete_secret("secret-a", 1) is True


def test_windows_authority_binding_is_durable_conflict_safe_and_retirable() -> None:
    backend = FakePersistentWinVault()
    store = WindowsCredentialStore(backend)
    store.provision_secret("secret-a", 1, RAW_SECRET)
    authority = hashlib.sha256(b"project-a-authority").hexdigest()
    conflicting = hashlib.sha256(b"project-b-authority").hexdigest()

    store.bind_authority(
        secret_ref="secret-a",
        generation=1,
        authority_fingerprint=authority,
    )
    restarted = WindowsCredentialStore(backend)

    assert restarted.authority_matches(
        secret_ref="secret-a",
        generation=1,
        authority_fingerprint=authority,
    )
    assert not restarted.authority_matches(
        secret_ref="secret-a",
        generation=1,
        authority_fingerprint=conflicting,
    )
    with pytest.raises(ProtectedCredentialStoreError, match="authority binding conflicts"):
        restarted.bind_authority(
            secret_ref="secret-a",
            generation=1,
            authority_fingerprint=conflicting,
        )
    with pytest.raises(ProtectedCredentialStoreError, match="cannot be retired"):
        restarted.delete_authority("secret-a", 1)

    assert restarted.delete_secret("secret-a", 1) is True
    assert restarted.authority_matches(
        secret_ref="secret-a",
        generation=1,
        authority_fingerprint=authority,
    )
    assert restarted.delete_authority("secret-a", 1) is True
    assert not restarted.authority_matches(
        secret_ref="secret-a",
        generation=1,
        authority_fingerprint=authority,
    )


def test_windows_handle_operation_is_idempotent_and_reconcilable() -> None:
    backend = FakePersistentWinVault()
    store = WindowsCredentialStore(backend)
    store.provision_secret("secret-a", 1, RAW_SECRET)
    expires_at = NOW + timedelta(minutes=5)
    request = dict(
        operation_id="credential-lease-00000001",
        secret_ref="secret-a",
        generation=1,
        project_id=PROJECT,
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        expires_at=expires_at,
    )

    first = store.issue_handle(**request)
    second = store.issue_handle(**request)
    reconciled = store.reconcile_handle(**request)

    assert second == first
    assert reconciled == first
    assert len(store._handles) == 1
    with pytest.raises(ProtectedCredentialStoreError, match="operation identity conflicts"):
        store.issue_handle(
            **{**request, "scopes": frozenset({"checks:read"})}
        )

    store.revoke_handles("secret-a", 1)
    assert store.reconcile_handle(**request) is None
