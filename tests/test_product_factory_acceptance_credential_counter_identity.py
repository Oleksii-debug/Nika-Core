from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialBrokerSnapshot,
    SecretRef,
)

NOW = datetime(2026, 8, 21, 13, 45, tzinfo=UTC)


@dataclass(slots=True)
class _ProtectedStore:
    material: set[tuple[str, int]] = field(default_factory=set)

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.material

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
        del project_id, audience, scopes, expires_at
        return f"handle:{secret_ref}:{generation}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def _broker() -> tuple[CredentialBroker, _ProtectedStore]:
    store = _ProtectedStore({("secret-a", 1)})
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "repository automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=NOW,
    )
    return broker, store


def test_pf7_restore_rejects_lease_counter_rollback_and_identity_reuse() -> None:
    broker, store = _broker()
    first = broker.issue_lease(
        project_id="project-a",
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    snapshot = broker.snapshot()
    assert first.lease_id == "credential-lease-00000001"
    assert snapshot.next_lease == 2

    rolled_back = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        snapshot.audit_events,
        1,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(store).restore(rolled_back)


@pytest.mark.parametrize(
    ("next_lease", "next_event"),
    [
        (True, 1),
        (1, True),
        (1.5, 1),
        (1, 1.5),
    ],
)
def test_pf7_snapshot_counters_require_exact_integers(
    next_lease: object,
    next_event: object,
) -> None:
    with pytest.raises(CredentialBrokerError):
        CredentialBrokerSnapshot((), (), (), next_lease, next_event)  # type: ignore[arg-type]
