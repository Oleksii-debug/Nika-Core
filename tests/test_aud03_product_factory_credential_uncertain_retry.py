from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import CredentialBroker, SecretRef

NOW = datetime(2026, 8, 23, 13, 50, tzinfo=UTC)
SECRET_REF = "secret:aud03-uncertain"
PROJECT_ID = "project:aud03-uncertain"


@dataclass(slots=True)
class _EffectThenErrorStore:
    material: set[tuple[str, int]] = field(default_factory=lambda: {(SECRET_REF, 1)})
    issued_handles: list[str] = field(default_factory=list)

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
        del secret_ref, generation, project_id, audience, scopes, expires_at
        handle_ref = f"external-handle-{len(self.issued_handles) + 1}"
        self.issued_handles.append(handle_ref)
        if len(self.issued_handles) == 1:
            raise RuntimeError("simulated uncertain result after external handle creation")
        return handle_ref

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def _broker(store: _EffectThenErrorStore) -> CredentialBroker:
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            secret_ref=SECRET_REF,
            project_id=PROJECT_ID,
            provider="github",
            purpose="repository automation",
            scopes=frozenset({"repo:read"}),
            allowed_audiences=frozenset({"github-api"}),
        ),
        now=NOW,
    )
    return broker


def test_uncertain_handle_creation_retry_does_not_duplicate_external_effect() -> None:
    store = _EffectThenErrorStore()
    broker = _broker(store)

    with pytest.raises(RuntimeError, match="uncertain result"):
        broker.issue_lease(
            project_id=PROJECT_ID,
            secret_ref=SECRET_REF,
            audience="github-api",
            scopes=frozenset({"repo:read"}),
            now=NOW,
        )

    # The external side effect happened, but no lease/audit identity was committed.
    after_uncertain = broker.snapshot()
    assert store.issued_handles == ["external-handle-1"]
    assert after_uncertain.next_lease == 1
    assert all(event.action != "lease" for event in after_uncertain.audit_events)

    # A normal retry has no idempotency/reconciliation identity to present to the
    # protected store, so production currently creates a second external handle.
    lease = broker.issue_lease(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )

    assert lease.handle_ref == "external-handle-1"
    assert store.issued_handles == ["external-handle-1"]
