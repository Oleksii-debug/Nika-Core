from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    SecretRef,
)

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.issued = 0
        self.available = True

    def contains(self, secret_ref: str, generation: int) -> bool:
        return self.available and secret_ref == "secret-a" and generation == 1

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
        self.issued += 1
        return f"opaque-handle-{self.issued}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        pass


def _broker() -> tuple[CredentialBroker, _Store]:
    store = _Store()
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "test automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=_NOW,
    )
    return broker, store


def _issue(broker: CredentialBroker, ttl_seconds: int = 5):
    return broker.issue_lease(
        project_id="project-a",
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=_NOW,
        ttl_seconds=ttl_seconds,
    )


@pytest.mark.parametrize("ttl", [True, False, 1.0, 1.5, "5", None, float("nan")])
def test_invalid_ttl_rejected_before_protected_handle_or_audit(ttl: object) -> None:
    broker, store = _broker()
    initial = broker.snapshot()

    with pytest.raises(CredentialBrokerError, match="ttl must be an integer"):
        _issue(broker, ttl)  # type: ignore[arg-type]

    assert store.issued == 0
    assert broker.snapshot() == initial


@pytest.mark.parametrize("ttl", [0, -1, 901])
def test_integer_ttl_limits_still_apply_without_issuing_handle(ttl: int) -> None:
    broker, store = _broker()

    with pytest.raises(CredentialBrokerError, match="ttl"):
        _issue(broker, ttl)

    assert store.issued == 0


def test_backdated_use_cannot_authorize_or_audit_a_lease() -> None:
    broker, store = _broker()
    lease = _issue(broker)
    before = broker.snapshot()

    with pytest.raises(CredentialBrokerError, match="before issuance"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id="project-a",
            scope="repo:read",
            now=_NOW - timedelta(microseconds=1),
        )

    assert store.issued == 1
    assert broker.snapshot() == before

    evidence = broker.authorize_use(
        lease_id=lease.lease_id,
        project_id="project-a",
        scope="repo:read",
        now=_NOW,
    )
    assert evidence.lease_id == lease.lease_id
    assert evidence.used_at == _NOW


def test_lease_still_expires_at_exact_boundary() -> None:
    broker, _ = _broker()
    lease = _issue(broker)

    broker.authorize_use(
        lease_id=lease.lease_id,
        project_id="project-a",
        scope="repo:read",
        now=_NOW + timedelta(seconds=4),
    )
    with pytest.raises(CredentialBrokerError, match="expired"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id="project-a",
            scope="repo:read",
            now=_NOW + timedelta(seconds=5),
        )

def test_removed_protected_material_cannot_authorize_or_audit_stale_handle() -> None:
    broker, store = _broker()
    lease = _issue(broker)
    before = broker.snapshot()
    store.available = False

    with pytest.raises(CredentialBrokerError, match="material is unavailable"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id="project-a",
            scope="repo:read",
            now=_NOW + timedelta(seconds=1),
        )

    assert broker.snapshot() == before
    store.available = True
    with pytest.raises(CredentialBrokerError, match="unknown or invalidated"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id="project-a",
            scope="repo:read",
            now=_NOW + timedelta(seconds=2),
        )

    replacement = _issue(broker)
    assert replacement.lease_id != lease.lease_id
