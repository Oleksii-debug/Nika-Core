from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialLease,
    CredentialState,
    IdentityRef,
    SecretRef,
)

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.issued = 0
        self.available = True
        self.last_scopes: frozenset[str] | None = None

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
        self.last_scopes = scopes
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


def test_mutable_delegated_scopes_cannot_expand_an_issued_lease() -> None:
    broker, store = _broker()
    requested = {"repo:read"}
    lease = broker.issue_lease(
        project_id="project-a",
        secret_ref="secret-a",
        audience="github-api",
        scopes=requested,  # type: ignore[arg-type]
        now=_NOW,
    )
    requested.add("repo:write")

    assert lease.scopes == frozenset({"repo:read"})
    assert store.last_scopes == lease.scopes
    with pytest.raises(CredentialBrokerError, match="does not authorize"):
        broker.authorize_use(
            lease_id=lease.lease_id,
            project_id="project-a",
            scope="repo:write",
            now=_NOW + timedelta(seconds=1),
        )
    assert broker.authorize_use(
        lease_id=lease.lease_id,
        project_id="project-a",
        scope="repo:read",
        now=_NOW + timedelta(seconds=2),
    ).scope == "repo:read"


def test_registered_secret_scopes_and_audiences_snapshot_caller_sets() -> None:
    scopes = {"repo:read"}
    audiences = {"github-api"}
    secret = SecretRef(
        "secret-a",
        "project-a",
        "github",
        "test automation",
        scopes,  # type: ignore[arg-type]
        audiences,  # type: ignore[arg-type]
    )
    broker = CredentialBroker(_Store())
    broker.register_secret(secret, now=_NOW)
    scopes.add("repo:write")
    audiences.add("unapproved-service")

    assert secret.scopes == frozenset({"repo:read"})
    assert secret.allowed_audiences == frozenset({"github-api"})
    with pytest.raises(CredentialBrokerError, match="scopes exceed"):
        broker.issue_lease(
            project_id="project-a",
            secret_ref="secret-a",
            audience="github-api",
            scopes=frozenset({"repo:write"}),
            now=_NOW,
        )
    with pytest.raises(CredentialBrokerError, match="audience is not allowed"):
        broker.issue_lease(
            project_id="project-a",
            secret_ref="secret-a",
            audience="unapproved-service",
            scopes=frozenset({"repo:read"}),
            now=_NOW,
        )


def test_identity_refs_snapshot_mutable_input_before_registration() -> None:
    refs = ["secret-a"]
    identity = IdentityRef(
        "identity-a",
        "project-a",
        "github",
        "subject-a",
        refs,  # type: ignore[arg-type]
    )
    refs.append("foreign-secret")
    broker, _ = _broker()
    broker.register_identity(identity)

    assert broker.get_identity(project_id="project-a", identity_ref="identity-a").secret_refs == (
        "secret-a",
    )


@pytest.mark.parametrize("scopes", [["repo:read"], "repo:read", {"repo:read": True}, {1}])
def test_malformed_lease_scope_carrier_rejected_before_handle(scopes: object) -> None:
    broker, store = _broker()
    initial = broker.snapshot()

    with pytest.raises(CredentialBrokerError, match="credential lease scopes"):
        broker.issue_lease(
            project_id="project-a",
            secret_ref="secret-a",
            audience="github-api",
            scopes=scopes,  # type: ignore[arg-type]
            now=_NOW,
        )
    assert store.issued == 0
    assert broker.snapshot() == initial


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.5, "1"])
def test_ambiguous_secret_generation_is_rejected(generation: object) -> None:
    with pytest.raises(CredentialBrokerError, match="positive integer"):
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "test automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
            generation,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("state", ["REVOKED", "revoked ", None, 1])
def test_invalid_secret_state_does_not_become_active(state: object) -> None:
    with pytest.raises(CredentialBrokerError, match="credential state is invalid"):
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "test automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
            state=state,  # type: ignore[arg-type]
        )


def test_serialized_revoked_state_is_normalized_before_lease_admission() -> None:
    broker = CredentialBroker(store := _Store())
    secret = SecretRef(
        "secret-a",
        "project-a",
        "github",
        "test automation",
        frozenset({"repo:read"}),
        frozenset({"github-api"}),
        state="revoked",  # type: ignore[arg-type]
    )
    assert secret.state is CredentialState.REVOKED
    broker.register_secret(secret, now=_NOW)
    with pytest.raises(CredentialBrokerError, match="credential is revoked"):
        _issue(broker)
    assert store.issued == 0


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.5, "1"])
def test_ambiguous_direct_lease_generation_is_rejected(generation: object) -> None:
    with pytest.raises(CredentialBrokerError, match="lease generation must be"):
        CredentialLease(
            "lease-a",
            "secret-a",
            "project-a",
            "github-api",
            frozenset({"repo:read"}),
            generation,  # type: ignore[arg-type]
            "opaque-handle",
            _NOW,
            _NOW + timedelta(seconds=5),
        )
