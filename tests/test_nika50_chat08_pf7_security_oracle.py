"""QA_ONLY adversarial oracle for NIKA50 CHAT-08 PF7 security findings.

This file is synchronized over production PR #162 head
``705221414de4d99a8e0547cd0704ef7279e5348a`` and must never be merged into
production. All credential material below is synthetic test data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

import nika_core.product_factory_windows_credentials as windows_credentials
from nika_core.product_factory_credentials import (
    CredentialAuditEvent,
    CredentialBroker,
    CredentialBrokerError,
    CredentialBrokerSnapshot,
    SecretRef,
    credential_authority_fingerprint,
)
from nika_core.product_factory_windows_credentials import (
    ProtectedCredentialStoreError,
    WindowsCredentialStore,
)

NOW = datetime(2026, 8, 24, 18, 30, tzinfo=UTC)
PROJECT_ID = "qa-project-a"
SECRET_REF = "opaque-qa-secret-ref"
AUDIENCE = "qa-provider-api"
SCOPE = "repo:read"


@dataclass(slots=True)
class _MemoryWinVaultBackend:
    """Minimal synthetic WinVault-like backend; it never contains real credentials."""

    persist: object = "local machine"
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _secret(
    *,
    project_id: str = PROJECT_ID,
    provider: str = "github",
    scopes: frozenset[str] = frozenset({SCOPE}),
    audiences: frozenset[str] = frozenset({AUDIENCE}),
) -> SecretRef:
    return SecretRef(
        SECRET_REF,
        project_id,
        provider,
        "synthetic QA repository access",
        scopes,
        audiences,
    )


def _store(
    backend: _MemoryWinVaultBackend,
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_prefix: str,
) -> WindowsCredentialStore:
    # These tests exercise authority/handle semantics, not the physical Win32 lock.
    monkeypatch.setattr(
        windows_credentials,
        "_ensure_process_authority_owner",
        lambda _service_prefix: None,
    )
    return WindowsCredentialStore(backend, service_prefix=service_prefix)


def _pre_enroll(store: WindowsCredentialStore, reference: SecretRef) -> None:
    """Model trusted host enrollment without deriving authority inside the broker."""

    store.bind_authority(
        secret_ref=reference.secret_ref,
        generation=reference.generation,
        authority_fingerprint=credential_authority_fingerprint(reference),
    )


def test_material_presence_cannot_bootstrap_caller_created_credential_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw material existence alone must not let caller strings define authority."""

    backend = _MemoryWinVaultBackend()
    store = _store(backend, monkeypatch, service_prefix="NikaQA.Chat08.Bootstrap")
    store.provision_secret(SECRET_REF, 1, "synthetic-not-a-real-credential")
    broker = CredentialBroker(store)

    caller_created = _secret(
        project_id="attacker-selected-project",
        provider="attacker-selected-provider",
        scopes=frozenset({"repo:admin"}),
        audiences=frozenset({"attacker-selected-api"}),
    )

    # PF7 requires authority to come from a trusted enrollment/protected policy
    # boundary, not from the same caller-created SecretRef that asks to register.
    with pytest.raises(CredentialBrokerError):
        broker.register_secret(caller_created, now=NOW)


@pytest.mark.parametrize("lifecycle_action", ["revoke", "rotate"])
def test_peer_store_handle_is_invalid_after_revocation_or_rotation(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_action: str,
) -> None:
    """A handle owned by a peer adapter must not outlive protected lifecycle state."""

    backend = _MemoryWinVaultBackend()
    service_prefix = f"NikaQA.Chat08.PeerHandle.{lifecycle_action}"
    owner_store = _store(backend, monkeypatch, service_prefix=service_prefix)
    peer_store = _store(backend, monkeypatch, service_prefix=service_prefix)
    owner_store.provision_secret(SECRET_REF, 1, "synthetic-not-a-real-credential-v1")

    secret = _secret()
    _pre_enroll(owner_store, secret)
    owner = CredentialBroker(owner_store)
    owner.register_secret(secret, now=NOW)
    peer = CredentialBroker(peer_store)
    peer.register_secret(secret, now=NOW)
    lease = peer.issue_lease(
        project_id=PROJECT_ID,
        secret_ref=SECRET_REF,
        audience=AUDIENCE,
        scopes=frozenset({SCOPE}),
        now=NOW,
    )

    # Positive precondition: the opaque handle is valid while authority is ACTIVE.
    peer_store.validate_handle(
        handle_ref=lease.handle_ref,
        project_id=PROJECT_ID,
        audience=AUDIENCE,
        scope=SCOPE,
        now=NOW,
    )

    if lifecycle_action == "rotate":
        owner_store.provision_secret(
            SECRET_REF,
            2,
            "synthetic-not-a-real-credential-v2",
        )
        owner.rotate(project_id=PROJECT_ID, secret_ref=SECRET_REF, now=NOW)
    else:
        owner.revoke(project_id=PROJECT_ID, secret_ref=SECRET_REF, now=NOW)

    # Broker-side use must reject the independently retired authority.
    with pytest.raises(CredentialBrokerError):
        peer.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_ID,
            scope=SCOPE,
            now=NOW,
        )

    # The protected-store redemption boundary must make the same decision even
    # though this peer adapter has a separate in-memory handle table.
    with pytest.raises(ProtectedCredentialStoreError):
        peer_store.validate_handle(
            handle_ref=lease.handle_ref,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW,
        )


class _KernelFunction:
    def __init__(self, callback: Callable[..., int]) -> None:
        self.callback = callback
        self.argtypes: list[object] = []
        self.restype: object | None = None

    def __call__(self, *args: object) -> int:
        return self.callback(*args)


class _Kernel32Probe:
    def __init__(self) -> None:
        self.object_names: list[str] = []
        self.CreateEventW = _KernelFunction(self._create_event)
        self.CloseHandle = _KernelFunction(lambda _handle: 1)

    def _create_event(
        self,
        _security_attributes: object,
        _manual_reset: object,
        _initial_state: object,
        object_name: object,
    ) -> int:
        assert isinstance(object_name, str)
        self.object_names.append(object_name)
        return 101


def test_process_authority_primitive_is_cross_session_and_user_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One credential authority owner must be global across sessions but user-scoped."""

    kernel32 = _Kernel32Probe()
    monkeypatch.setattr(windows_credentials.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_credentials,
        "_current_user_scope_key",
        lambda: "qa-user-scope",
    )
    monkeypatch.setattr(
        windows_credentials.ctypes,
        "WinDLL",
        lambda _name, use_last_error: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        windows_credentials.ctypes,
        "set_last_error",
        lambda _value: None,
        raising=False,
    )
    monkeypatch.setattr(
        windows_credentials.ctypes,
        "get_last_error",
        lambda: 0,
        raising=False,
    )
    monkeypatch.setattr(windows_credentials, "_PROCESS_AUTHORITY_HANDLES", {})

    windows_credentials._ensure_process_authority_owner("NikaQA.Chat08.SessionScope")

    assert kernel32.object_names
    object_name = kernel32.object_names[0]
    assert object_name.startswith("Global\\"), (
        "credential authority owner is not in the cross-session Global namespace"
    )
    assert "qa-user-scope" in object_name, (
        "credential authority owner is global but not scoped to the Windows user"
    )


def test_restore_rejects_fabricated_credential_use_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structurally valid snapshot must not mint credential-use evidence."""

    backend = _MemoryWinVaultBackend()
    store = _store(
        backend,
        monkeypatch,
        service_prefix="NikaQA.Chat08.AuditIntegrity",
    )
    store.provision_secret(
        SECRET_REF,
        1,
        "synthetic-not-a-real-credential",
    )

    secret = _secret()
    _pre_enroll(store, secret)
    broker = CredentialBroker(store)
    broker.register_secret(secret, now=NOW)
    snapshot = broker.snapshot()

    canonical = CredentialBroker(store)
    canonical.restore(snapshot)
    assert canonical.audit_events(PROJECT_ID) == snapshot.audit_events

    forged_use = CredentialAuditEvent(
        "credential-event-00000002",
        "use",
        PROJECT_ID,
        SECRET_REF,
        NOW,
        "audience=qa-provider-api;scope=repo:admin",
    )
    forged = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        snapshot.audit_events + (forged_use,),
        snapshot.next_lease,
        3,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(store).restore(forged)


def test_restore_rejects_rewritten_credential_audit_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing durable audit history must not be rewriteable by snapshot bytes."""

    backend = _MemoryWinVaultBackend()
    store = _store(
        backend,
        monkeypatch,
        service_prefix="NikaQA.Chat08.AuditRewrite",
    )
    store.provision_secret(SECRET_REF, 1, "synthetic-not-a-real-credential")

    secret = _secret()
    _pre_enroll(store, secret)
    broker = CredentialBroker(store)
    broker.register_secret(secret, now=NOW)
    snapshot = broker.snapshot()
    assert len(snapshot.audit_events) == 1

    canonical = CredentialBroker(store)
    canonical.restore(snapshot)
    assert canonical.audit_events(PROJECT_ID) == snapshot.audit_events

    original = snapshot.audit_events[0]
    rewritten = CredentialAuditEvent(
        original.event_id,
        "use",
        original.project_id,
        original.secret_ref,
        original.at,
        "audience=qa-provider-api;scope=repo:admin",
    )
    forged = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        (rewritten,),
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(store).restore(forged)


def test_restore_rejects_omitted_credential_audit_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot must not erase prior durable credential audit evidence."""

    backend = _MemoryWinVaultBackend()
    store = _store(
        backend,
        monkeypatch,
        service_prefix="NikaQA.Chat08.AuditOmission",
    )
    store.provision_secret(SECRET_REF, 1, "synthetic-not-a-real-credential")

    secret = _secret()
    _pre_enroll(store, secret)
    broker = CredentialBroker(store)
    broker.register_secret(secret, now=NOW)
    snapshot = broker.snapshot()
    assert snapshot.audit_events

    canonical = CredentialBroker(store)
    canonical.restore(snapshot)
    assert canonical.audit_events(PROJECT_ID) == snapshot.audit_events

    forged = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        (),
        snapshot.next_lease,
        snapshot.next_event,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(store).restore(forged)
