from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_credentials import (
    CredentialAuditEvent,
    CredentialBroker,
    CredentialBrokerError,
    CredentialBrokerSnapshot,
    SecretRef,
)

NOW = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
PROJECT_ID = "project-a"
SECRET_REF = "secret-a"


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
        if not self.contains(secret_ref, generation):
            raise AssertionError("missing protected material")
        return f"handle:{secret_ref}:{generation}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def _broker() -> tuple[CredentialBroker, _ProtectedStore]:
    store = _ProtectedStore({(SECRET_REF, 1)})
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            SECRET_REF,
            PROJECT_ID,
            "github",
            "repository automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=NOW,
    )
    return broker, store


def _with_next_event(
    snapshot: CredentialBrokerSnapshot,
    next_event: int,
) -> CredentialBrokerSnapshot:
    return CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        snapshot.audit_events,
        snapshot.next_lease,
        next_event,
    )


@pytest.mark.parametrize("next_event", [1])
def test_restore_rejects_audit_counter_rollback_or_reuse(next_event: int) -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()

    with pytest.raises(CredentialBrokerError, match="counter was rolled back"):
        CredentialBroker(store).restore(_with_next_event(snapshot, next_event))


def test_restore_rejects_counter_equal_to_highest_persisted_event() -> None:
    broker, store = _broker()
    broker.issue_lease(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW + timedelta(seconds=1),
    )
    snapshot = broker.snapshot()
    assert snapshot.audit_events[-1].event_id == "credential-event-00000002"

    with pytest.raises(CredentialBrokerError, match="counter was rolled back"):
        CredentialBroker(store).restore(_with_next_event(snapshot, 2))


def test_restore_preserves_monotonic_audit_identity_after_restart() -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    assert snapshot.next_event == 2

    restored = CredentialBroker(store)
    restored.restore(snapshot)
    restored.revoke(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        now=NOW + timedelta(seconds=1),
    )

    assert tuple(event.event_id for event in restored.audit_events(PROJECT_ID)) == (
        "credential-event-00000001",
        "credential-event-00000002",
    )
    assert restored.snapshot().next_event == 3


def test_restore_accepts_canonical_audit_identity_beyond_eight_digits() -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    first = snapshot.audit_events[0]
    late = CredentialAuditEvent(
        "credential-event-100000000",
        first.action,
        first.project_id,
        first.secret_ref,
        first.at,
        first.detail,
    )
    stretched = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        (late,),
        snapshot.next_lease,
        100000001,
    )

    restored = CredentialBroker(store)
    restored.restore(stretched)
    restored.revoke(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        now=NOW + timedelta(seconds=1),
    )

    assert tuple(event.event_id for event in restored.audit_events(PROJECT_ID)) == (
        "credential-event-100000000",
        "credential-event-100000001",
    )


def test_restore_rejects_noncanonical_or_nonmonotonic_audit_identities() -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    first = snapshot.audit_events[0]
    second = CredentialAuditEvent(
        "credential-event-00000002",
        "lease",
        PROJECT_ID,
        SECRET_REF,
        NOW + timedelta(seconds=1),
        "opaque lease issued",
    )

    reversed_snapshot = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        (second, first),
        snapshot.next_lease,
        3,
    )
    with pytest.raises(CredentialBrokerError, match="events are not monotonic"):
        CredentialBroker(store).restore(reversed_snapshot)

    malformed = CredentialAuditEvent(
        "credential-event-not-a-counter",
        first.action,
        first.project_id,
        first.secret_ref,
        first.at,
        first.detail,
    )
    malformed_snapshot = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        (malformed,),
        snapshot.next_lease,
        2,
    )
    with pytest.raises(CredentialBrokerError, match="invalid audit event identity"):
        CredentialBroker(store).restore(malformed_snapshot)
