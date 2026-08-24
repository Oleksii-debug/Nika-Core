"""AUD02 QA_ONLY oracle for PF7 candidate-controlled credential authority."""

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

NOW = datetime(2026, 8, 23, 20, 20, tzinfo=UTC)
SECRET_REF = "opaque-secret-a"


class _ProtectedStore:
    def __init__(self) -> None:
        self.authority: dict[tuple[str, int], str] = {}

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) == (SECRET_REF, 1)

    def bind_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> None:
        key = (secret_ref, generation)
        existing = self.authority.get(key)
        if existing is not None and existing != authority_fingerprint:
            raise RuntimeError("conflicting protected authority")
        self.authority[key] = authority_fingerprint

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool:
        return self.authority.get((secret_ref, generation)) == authority_fingerprint

    def issue_handle(self, **kwargs) -> str:
        del kwargs
        return "opaque-handle"

    def reconcile_handle(self, **kwargs) -> str | None:
        del kwargs
        return None

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def _snapshot(store: _ProtectedStore) -> CredentialBrokerSnapshot:
    broker = CredentialBroker(store)
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


def test_restore_rejects_scope_and_audience_expansion_against_protected_authority() -> None:
    store = _ProtectedStore()
    snapshot = _snapshot(store)
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
        CredentialBroker(store).restore(forged)


def test_restore_rejects_project_substitution_even_with_candidate_consistent_audit() -> None:
    store = _ProtectedStore()
    snapshot = _snapshot(store)
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
        CredentialBroker(store).restore(forged)
