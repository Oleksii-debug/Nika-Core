from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialLease,
    CredentialUseEvidence,
    SecretRef,
)

NOW = datetime(2026, 8, 23, 13, 45, tzinfo=UTC)
PROJECT_A = "product-a"
PROJECT_B = "product-b"
SECRET_REF = "opaque-secret-a"


@dataclass(slots=True)
class _ProtectedStore:
    material: set[tuple[str, int]] = field(default_factory=set)
    authorities: dict[tuple[str, int], str] = field(default_factory=dict)
    handles: dict[str, tuple[str, int]] = field(default_factory=dict)

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.material

    def bind_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> None:
        key = (secret_ref, generation)
        existing = self.authorities.get(key)
        if existing is not None and existing != authority_fingerprint:
            raise AssertionError("authority conflict")
        self.authorities[key] = authority_fingerprint

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool:
        return self.authorities.get((secret_ref, generation)) == authority_fingerprint

    def issue_handle(
        self,
        *,
        operation_id: str,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str:
        del project_id, audience, scopes, expires_at
        if not self.contains(secret_ref, generation):
            raise AssertionError("missing protected material")
        self.handles.setdefault(operation_id, (secret_ref, generation))
        return f"opaque-handle:{operation_id}"

    def reconcile_handle(
        self,
        *,
        operation_id: str,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str | None:
        del secret_ref, generation, project_id, audience, scopes, expires_at
        if operation_id not in self.handles:
            return None
        return f"opaque-handle:{operation_id}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        for operation_id, identity in list(self.handles.items()):
            if identity == (secret_ref, generation):
                del self.handles[operation_id]


def _secret(*, state_generation: int = 1) -> SecretRef:
    return SecretRef(
        SECRET_REF,
        PROJECT_A,
        "github",
        "repository automation",
        frozenset({"repo:read", "checks:read"}),
        frozenset({"github-api"}),
        state_generation,
    )


def _broker() -> tuple[CredentialBroker, _ProtectedStore]:
    store = _ProtectedStore({(SECRET_REF, 1)})
    broker = CredentialBroker(store)
    broker.register_secret(_secret(), now=NOW)
    return broker, store


def test_secret_reference_access_is_exact_not_enumerable() -> None:
    broker, _store = _broker()

    assert not hasattr(broker, "list_project_secret_refs")
    assert broker.get_secret_ref(project_id=PROJECT_A, secret_ref=SECRET_REF) == _secret()

    with pytest.raises(CredentialBrokerError, match="unavailable for project"):
        broker.get_secret_ref(project_id=PROJECT_B, secret_ref=SECRET_REF)


def test_restore_fails_closed_when_active_protected_generation_is_missing() -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    store.material.clear()

    with pytest.raises(CredentialBrokerError, match="protected store.*unavailable"):
        CredentialBroker(store).restore(snapshot)


def test_restore_allows_missing_material_for_already_revoked_reference() -> None:
    broker, store = _broker()
    broker.revoke(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)
    snapshot = broker.snapshot()
    store.material.clear()

    restored = CredentialBroker(store)
    restored.restore(snapshot)

    secret = restored.get_secret_ref(project_id=PROJECT_A, secret_ref=SECRET_REF)
    assert secret.state.value == "revoked"


def test_secret_generation_rejects_boolean_identity() -> None:
    with pytest.raises(CredentialBrokerError, match="generation.*positive integer"):
        _secret(state_generation=True)  # type: ignore[arg-type]


def test_lease_generation_rejects_boolean_identity() -> None:
    with pytest.raises(CredentialBrokerError, match="generation.*positive integer"):
        CredentialLease(
            "lease-a",
            SECRET_REF,
            PROJECT_A,
            "github-api",
            frozenset({"repo:read"}),
            True,  # type: ignore[arg-type]
            "opaque-handle",
            NOW,
            datetime(2026, 8, 23, 14, 0, tzinfo=UTC),
        )


def test_use_evidence_generation_rejects_boolean_identity() -> None:
    with pytest.raises(CredentialBrokerError, match="generation.*positive integer"):
        CredentialUseEvidence(
            "credential-event-00000001",
            "lease-a",
            SECRET_REF,
            PROJECT_A,
            "github-api",
            "repo:read",
            True,  # type: ignore[arg-type]
            NOW,
        )


def test_lease_ttl_rejects_boolean_alias() -> None:
    broker, _store = _broker()

    with pytest.raises(CredentialBrokerError, match="ttl.*positive integer"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref=SECRET_REF,
            audience="github-api",
            scopes=frozenset({"repo:read"}),
            now=NOW,
            ttl_seconds=True,  # type: ignore[arg-type]
        )
