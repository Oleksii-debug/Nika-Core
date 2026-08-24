"""QA_ONLY adversarial oracle for NIKA50 CHAT-08 PF7 security findings.

This file is intentionally based on production PR #162 head
``dc934da20031f2caf85cfb1519abadf9940c04e0`` and must never be merged into
production.  All credential material below is synthetic test data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

import nika_core.product_factory_windows_credentials as windows_credentials
from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    SecretRef,
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

    # Broker-side use already sees the independently retired authority.
    with pytest.raises(CredentialBrokerError):
        peer.authorize_use(
            lease_id=lease.lease_id,
            project_id=PROJECT_ID,
            scope=SCOPE,
            now=NOW,
        )

    # The protected-store redemption boundary must make the same decision.  On
    # dc934da... validate_handle() checks only raw material existence, so a peer
    # adapter can currently continue accepting the stale handle.
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


def test_process_authority_primitive_is_not_limited_to_current_logon_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-per-user authority owner cannot use the per-session Local namespace."""

    kernel32 = _Kernel32Probe()
    monkeypatch.setattr(windows_credentials.sys, "platform", "win32")
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
    assert not kernel32.object_names[0].startswith("Local\\"), (
        "credential authority owner is session-local rather than cross-session/user-scoped"
    )
