"""AUD02 QA_ONLY oracles for PF7 candidate-controlled credential authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialBrokerSnapshot,
    SecretRef,
)

NOW = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
SECRET_REF = "opaque-secret-a"


class _ProtectedStore:
    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) == (SECRET_REF, 1)

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
        del secret_ref, generation, project_id, audience, scopes, expires_at
        return "opaque-handle"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def _snapshot() -> CredentialBrokerSnapshot:
    broker = CredentialBroker(_ProtectedStore())
    broker.register_secret(
        SecretRef(
            SECRET_REF,
            "project-a",
            "github",
            "repository automation",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=NOW,
    )
    return broker.snapshot()


def test_restore_rejects_candidate_controlled_scope_and_audience_expansion() -> None:
    snapshot = _snapshot()
    expanded = replace(
        snapshot.secrets[0],
        scopes=frozenset({"repo:read", "repo:write"}),
        allowed_audiences=frozenset({"github-api", "attacker-api"}),
    )
    forged = CredentialBrokerSnapshot(
        (expanded,),
        snapshot.identities,
        snapshot.audit_events,
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(_ProtectedStore()).restore(forged)


def test_restore_rejects_candidate_controlled_project_substitution() -> None:
    snapshot = _snapshot()
    rebound_secret = replace(snapshot.secrets[0], project_id="project-b")
    rebound_audit = tuple(replace(event, project_id="project-b") for event in snapshot.audit_events)
    forged = CredentialBrokerSnapshot(
        (rebound_secret,),
        snapshot.identities,
        rebound_audit,
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(_ProtectedStore()).restore(forged)
