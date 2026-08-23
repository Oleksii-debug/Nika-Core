from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialState,
    SecretRef,
)

NOW = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
PROJECT_A = "product-a"
PROJECT_B = "product-b"
SECRET_REF = "secret-a"


@dataclass(slots=True)
class _AuthorityStore:
    material: set[tuple[str, int]] = field(default_factory=lambda: {(SECRET_REF, 1)})
    authorities: dict[tuple[str, int], str] = field(default_factory=dict)
    operation_handles: dict[str, tuple[str, tuple[object, ...]]] = field(default_factory=dict)
    issued_handles: list[str] = field(default_factory=list)
    fail_after_effect_once: bool = False
    fail_before_effect: bool = False
    fail_retirement_after_effect_once: bool = False

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
        if existing is None:
            self.authorities[key] = authority_fingerprint
        elif existing != authority_fingerprint:
            raise RuntimeError("authority conflict")

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool:
        return self.authorities.get((secret_ref, generation)) == authority_fingerprint

    def retire_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        current_authority_fingerprint: str,
        retired_authority_fingerprint: str,
    ) -> None:
        key = (secret_ref, generation)
        existing = self.authorities.get(key)
        if existing == retired_authority_fingerprint:
            return
        if existing != current_authority_fingerprint:
            raise RuntimeError("authority retirement conflict")
        self.authorities[key] = retired_authority_fingerprint
        if self.fail_retirement_after_effect_once:
            self.fail_retirement_after_effect_once = False
            raise RuntimeError("authority retirement acknowledgement lost")

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
        binding = (
            secret_ref,
            generation,
            project_id,
            audience,
            scopes,
            expires_at,
        )
        existing = self.operation_handles.get(operation_id)
        if existing is not None:
            handle, existing_binding = existing
            if existing_binding != binding:
                raise RuntimeError("operation conflict")
            return handle
        if self.fail_before_effect:
            raise RuntimeError("transport failed before effect")
        handle = f"opaque-handle-{len(self.issued_handles) + 1}"
        self.issued_handles.append(handle)
        self.operation_handles[operation_id] = (handle, binding)
        if self.fail_after_effect_once:
            self.fail_after_effect_once = False
            raise RuntimeError("transport acknowledgement lost")
        return handle

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
        existing = self.operation_handles.get(operation_id)
        if existing is None:
            return None
        handle, binding = existing
        if binding != (
            secret_ref,
            generation,
            project_id,
            audience,
            scopes,
            expires_at,
        ):
            raise RuntimeError("operation conflict")
        return handle

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        for operation_id, (_handle, binding) in list(self.operation_handles.items()):
            if binding[0] == secret_ref and binding[1] == generation:
                del self.operation_handles[operation_id]


def _secret(project_id: str = PROJECT_A) -> SecretRef:
    return SecretRef(
        SECRET_REF,
        project_id,
        "github",
        "repository automation",
        frozenset({"repo:read", "checks:read"}),
        frozenset({"github-api"}),
    )


def _broker(store: _AuthorityStore | None = None) -> tuple[CredentialBroker, _AuthorityStore]:
    protected_store = store or _AuthorityStore()
    broker = CredentialBroker(protected_store)
    broker.register_secret(_secret(), now=NOW)
    return broker, protected_store


def test_restore_rejects_forged_project_even_when_snapshot_is_self_consistent() -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    forged_secret = replace(snapshot.secrets[0], project_id=PROJECT_B)
    forged_events = tuple(replace(event, project_id=PROJECT_B) for event in snapshot.audit_events)
    forged = type(snapshot)(
        (forged_secret,),
        snapshot.identities,
        forged_events,
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError, match="authority does not match"):
        CredentialBroker(store).restore(forged)


@pytest.mark.parametrize(
    "replacement",
    [
        {"provider": "other-provider"},
        {"purpose": "other purpose"},
        {"scopes": frozenset({"repo:read", "repo:write"})},
        {"allowed_audiences": frozenset({"other-api"})},
    ],
)
def test_restore_rejects_forged_credential_policy(replacement: dict[str, object]) -> None:
    broker, store = _broker()
    snapshot = broker.snapshot()
    forged_secret = replace(snapshot.secrets[0], **replacement)
    forged = type(snapshot)(
        (forged_secret,),
        snapshot.identities,
        snapshot.audit_events,
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError, match="authority does not match"):
        CredentialBroker(store).restore(forged)


def test_restore_rejects_revocation_rollback_to_active_while_material_remains() -> None:
    broker, store = _broker()
    stale_active = broker.snapshot()

    broker.revoke(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)
    revoked = broker.snapshot()

    assert store.contains(SECRET_REF, 1)
    with pytest.raises(CredentialBrokerError, match="authority does not match"):
        CredentialBroker(store).restore(stale_active)

    restored = CredentialBroker(store)
    restored.restore(revoked)
    assert restored.get_secret_ref(
        project_id=PROJECT_A,
        secret_ref=SECRET_REF,
    ).state is CredentialState.REVOKED


def test_revocation_reconciles_lost_retirement_acknowledgement_on_exact_retry() -> None:
    store = _AuthorityStore(fail_retirement_after_effect_once=True)
    broker, store = _broker(store)

    with pytest.raises(RuntimeError, match="retirement acknowledgement lost"):
        broker.revoke(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)

    assert broker.get_secret_ref(project_id=PROJECT_A, secret_ref=SECRET_REF).state is CredentialState.ACTIVE
    broker.revoke(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)

    assert broker.get_secret_ref(
        project_id=PROJECT_A,
        secret_ref=SECRET_REF,
    ).state is CredentialState.REVOKED
    assert tuple(event.action for event in broker.audit_events(PROJECT_A)) == (
        "register",
        "revoke",
    )


def test_restore_rejects_rotation_rollback_to_old_active_generation() -> None:
    broker, store = _broker()
    stale_generation_one = broker.snapshot()
    store.material.add((SECRET_REF, 2))

    rotated = broker.rotate(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)
    current = broker.snapshot()

    assert rotated.generation == 2
    with pytest.raises(CredentialBrokerError, match="authority does not match"):
        CredentialBroker(store).restore(stale_generation_one)

    restored = CredentialBroker(store)
    restored.restore(current)
    active = restored.get_secret_ref(project_id=PROJECT_A, secret_ref=SECRET_REF)
    assert active.generation == 2
    assert active.state is CredentialState.ACTIVE


def test_post_effect_handle_failure_is_reconciled_without_duplicate_authority() -> None:
    store = _AuthorityStore(fail_after_effect_once=True)
    broker, store = _broker(store)

    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref=SECRET_REF,
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )

    assert store.issued_handles == [lease.handle_ref]
    assert lease.lease_id == "credential-lease-00000001"
    assert broker.snapshot().next_lease == 2
    assert tuple(event.action for event in broker.audit_events(PROJECT_A)) == (
        "register",
        "lease",
    )


def test_unreconciled_failure_blocks_different_request_until_original_retry() -> None:
    store = _AuthorityStore(fail_before_effect=True)
    broker, store = _broker(store)

    with pytest.raises(CredentialBrokerError, match="could not be reconciled"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref=SECRET_REF,
            audience="github-api",
            scopes=frozenset({"repo:read"}),
            now=NOW,
        )

    with pytest.raises(CredentialBrokerError, match="pending credential lease"):
        broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref=SECRET_REF,
            audience="github-api",
            scopes=frozenset({"checks:read"}),
            now=NOW,
        )

    store.fail_before_effect = False
    lease = broker.issue_lease(
        project_id=PROJECT_A,
        secret_ref=SECRET_REF,
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    assert lease.lease_id == "credential-lease-00000001"
    assert len(store.issued_handles) == 1


def test_concurrent_lease_issuance_keeps_unique_monotonic_identities() -> None:
    broker, store = _broker()

    def issue(scope: str) -> str:
        return broker.issue_lease(
            project_id=PROJECT_A,
            secret_ref=SECRET_REF,
            audience="github-api",
            scopes=frozenset({scope}),
            now=NOW,
        ).lease_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        lease_ids = set(executor.map(issue, ("repo:read", "checks:read")))

    assert lease_ids == {"credential-lease-00000001", "credential-lease-00000002"}
    assert len(store.issued_handles) == 2


def test_revoked_reference_still_requires_original_authority_on_restore() -> None:
    broker, store = _broker()
    broker.revoke(project_id=PROJECT_A, secret_ref=SECRET_REF, now=NOW)
    snapshot = broker.snapshot()
    store.material.clear()

    restored = CredentialBroker(store)
    restored.restore(snapshot)
    assert restored.get_secret_ref(
        project_id=PROJECT_A,
        secret_ref=SECRET_REF,
    ).state is CredentialState.REVOKED
