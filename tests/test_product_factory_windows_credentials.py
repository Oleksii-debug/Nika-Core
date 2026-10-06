from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

import nika_core.product_factory_windows_credentials as windows_credentials
from nika_core.product_factory_windows_credentials import (
    ProtectedCredentialStoreError,
    WindowsCredentialStore,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
RAW_SECRET = "unit-test-secret-never-log"
PROJECT = "product-a"
AUDIENCE = "github-api"
SCOPE = "repo:read"


@dataclass(slots=True)
class FakeWinVaultBackend:
    persist: object = "local machine"
    passwords: dict[tuple[str, str], str] = field(default_factory=dict)
    set_calls: list[tuple[str, str]] = field(default_factory=list)
    delete_calls: list[tuple[str, str]] = field(default_factory=list)
    read_error: Exception | None = None
    write_error: Exception | None = None
    delete_error: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.read_error is not None:
            raise self.read_error
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.set_calls.append((service, username))
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_calls.append((service, username))
        if (service, username) not in self.passwords:
            raise KeyError("missing")
        del self.passwords[(service, username)]


def new_store(backend: FakeWinVaultBackend | None = None) -> WindowsCredentialStore:
    return WindowsCredentialStore(backend or FakeWinVaultBackend())


def seeded_store() -> tuple[WindowsCredentialStore, FakeWinVaultBackend]:
    backend = FakeWinVaultBackend()
    store = new_store(backend)
    store.provision_secret("secret-a", 1, RAW_SECRET)
    return store, backend


def issue(store: WindowsCredentialStore, *, ttl_seconds: int = 300) -> str:
    return store.issue_handle(
        secret_ref="secret-a",
        generation=1,
        project_id=PROJECT,
        audience=AUDIENCE,
        scopes=frozenset({SCOPE, "checks:read"}),
        expires_at=NOW + timedelta(seconds=ttl_seconds),
    )


def test_target_is_deterministic_hashed_and_generation_scoped() -> None:
    store = new_store()

    target_1 = store._target("human-readable-secret-name", 1)
    target_2 = store._target("human-readable-secret-name", 2)

    assert target_1 != target_2
    assert target_1 == store._target("human-readable-secret-name", 1)
    assert "human-readable-secret-name" not in target_1
    assert target_1.startswith("NikaCore.ProductFactory.v1.")


def test_target_identity_remains_case_distinct_even_on_case_insensitive_windows() -> None:
    store = new_store()

    assert store._target("Secret-A", 1) != store._target("secret-a", 1)


def test_provision_is_write_once_and_exact_retry_is_idempotent() -> None:
    store, backend = seeded_store()

    store.provision_secret("secret-a", 1, RAW_SECRET)

    assert len(backend.set_calls) == 1
    assert store.contains("secret-a", 1)


def test_concurrent_same_generation_provisioning_allows_only_one_material() -> None:
    backend = FakeWinVaultBackend()
    store = new_store(backend)

    def provision(material: str) -> str:
        try:
            store.provision_secret("secret-a", 1, material)
        except ProtectedCredentialStoreError:
            return "rejected"
        return "stored"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(provision, ("first-material", "second-material")))

    assert sorted(outcomes) == ["rejected", "stored"]
    assert len(backend.set_calls) == 1
    target = store._target("secret-a", 1)
    assert backend.passwords[(target, "nika-core")] in {"first-material", "second-material"}


def test_provision_rejects_same_generation_with_different_material() -> None:
    store, backend = seeded_store()

    with pytest.raises(ProtectedCredentialStoreError, match="rotate generation"):
        store.provision_secret("secret-a", 1, "different-secret")

    assert len(backend.set_calls) == 1
    assert store.contains("secret-a", 1)


def test_windows_credential_blob_exact_byte_limit_is_accepted() -> None:
    store = new_store()

    store.provision_secret("ascii-limit", 1, "A" * 1280)
    store.provision_secret("surrogate-pair-limit", 1, "😀" * 640)

    assert store.contains("ascii-limit", 1)
    assert store.contains("surrogate-pair-limit", 1)


def test_utf16_surrogate_pair_over_limit_is_rejected() -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError, match="exceeds"):
        store.provision_secret("secret-a", 1, "😀" * 641)


def test_unpaired_unicode_surrogate_is_rejected_without_backend_call() -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError, match="valid Unicode"):
        store.provision_secret("secret-a", 1, "bad-\ud800")


def test_unencodable_secret_reference_is_rejected_before_backend_call() -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError, match="valid UTF-8"):
        store.contains("bad-\ud800", 1)


@pytest.mark.parametrize(
    "material",
    [
        "",
        "contains\x00nul",
        "a" * 1281,
    ],
)
def test_provision_rejects_invalid_or_oversized_material(material: str) -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError):
        store.provision_secret("secret-a", 1, material)


@pytest.mark.parametrize("bad_material", [None, b"bytes", 42])
def test_provision_rejects_non_text_material(bad_material: object) -> None:
    store = new_store()

    with pytest.raises(TypeError, match="material must be text"):
        store.provision_secret("secret-a", 1, bad_material)  # type: ignore[arg-type]


def test_backend_read_failure_is_sanitized() -> None:
    backend = FakeWinVaultBackend(read_error=RuntimeError(RAW_SECRET))
    store = new_store(backend)

    with pytest.raises(ProtectedCredentialStoreError) as captured:
        store.contains("secret-a", 1)

    assert RAW_SECRET not in str(captured.value)
    assert RAW_SECRET not in repr(captured.value)
    assert "RuntimeError" in str(captured.value)


def test_backend_write_failure_is_sanitized() -> None:
    backend = FakeWinVaultBackend(write_error=RuntimeError(RAW_SECRET))
    store = new_store(backend)

    with pytest.raises(ProtectedCredentialStoreError) as captured:
        store.provision_secret("secret-a", 1, RAW_SECRET)

    assert RAW_SECRET not in str(captured.value)
    assert RAW_SECRET not in repr(captured.value)


def test_backend_delete_failure_is_sanitized() -> None:
    store, backend = seeded_store()
    backend.delete_error = RuntimeError(RAW_SECRET)

    with pytest.raises(ProtectedCredentialStoreError) as captured:
        store.delete_secret("secret-a", 1)

    assert RAW_SECRET not in str(captured.value)
    assert RAW_SECRET not in repr(captured.value)


def test_contains_does_not_expose_material() -> None:
    store, _backend = seeded_store()

    assert store.contains("secret-a", 1) is True
    assert store.contains("secret-a", 2) is False
    assert RAW_SECRET not in repr(store)


def test_issue_handle_requires_existing_material() -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError, match="unavailable"):
        issue(store)


def test_issue_handle_rejects_empty_scope_set() -> None:
    store, _backend = seeded_store()

    with pytest.raises(ProtectedCredentialStoreError, match="scopes"):
        store.issue_handle(
            secret_ref="secret-a",
            generation=1,
            project_id=PROJECT,
            audience=AUDIENCE,
            scopes=frozenset(),
            expires_at=NOW + timedelta(minutes=5),
        )


def test_handles_are_unique_opaque_and_not_in_store_repr() -> None:
    store, _backend = seeded_store()

    first = issue(store)
    second = issue(store)

    assert first != second
    assert first.startswith("nika-credential-handle-")
    assert RAW_SECRET not in first
    assert "secret-a" not in first
    assert first not in repr(store)
    assert second not in repr(store)
    assert RAW_SECRET not in repr(store)


def test_valid_handle_validation_returns_reference_only_receipt() -> None:
    store, _backend = seeded_store()
    handle = issue(store)

    receipt = store.validate_handle(
        handle_ref=handle,
        project_id=PROJECT,
        audience=AUDIENCE,
        scope=SCOPE,
        now=NOW + timedelta(seconds=1),
    )

    assert receipt.secret_ref == "secret-a"
    assert receipt.scope == SCOPE
    assert RAW_SECRET not in repr(receipt)


def test_handle_cannot_cross_project_boundary() -> None:
    store, _backend = seeded_store()
    handle = issue(store)

    with pytest.raises(ProtectedCredentialStoreError, match="another project"):
        store.validate_handle(
            handle_ref=handle,
            project_id="product-b",
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )


def test_handle_cannot_cross_audience_boundary() -> None:
    store, _backend = seeded_store()
    handle = issue(store)

    with pytest.raises(ProtectedCredentialStoreError, match="audience"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience="unrelated-api",
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )


def test_handle_cannot_escalate_scope() -> None:
    store, _backend = seeded_store()
    handle = issue(store)

    with pytest.raises(ProtectedCredentialStoreError, match="scope"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope="repo:write",
            now=NOW + timedelta(seconds=1),
        )


def test_expired_handle_is_removed_and_cannot_be_replayed() -> None:
    store, _backend = seeded_store()
    handle = issue(store, ttl_seconds=5)

    with pytest.raises(ProtectedCredentialStoreError, match="expired"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=5),
        )
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=6),
        )


def test_revoke_handles_invalidates_only_matching_generation() -> None:
    store, _backend = seeded_store()
    store.provision_secret("secret-a", 2, "generation-two")
    handle_1 = issue(store)
    handle_2 = store.issue_handle(
        secret_ref="secret-a",
        generation=2,
        project_id=PROJECT,
        audience=AUDIENCE,
        scopes=frozenset({SCOPE}),
        expires_at=NOW + timedelta(minutes=5),
    )

    store.revoke_handles("secret-a", 1)

    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=handle_1,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )
    receipt = store.validate_handle(
        handle_ref=handle_2,
        project_id=PROJECT,
        audience=AUDIENCE,
        scope=SCOPE,
        now=NOW + timedelta(seconds=1),
    )
    assert receipt.generation == 2


def test_delete_is_idempotent_and_invalidates_handles() -> None:
    store, backend = seeded_store()
    handle = issue(store)

    assert store.delete_secret("secret-a", 1) is True
    assert store.delete_secret("secret-a", 1) is False
    assert len(backend.delete_calls) == 1
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )


def test_restart_keeps_os_material_but_never_restores_process_handles() -> None:
    backend = FakeWinVaultBackend()
    first = new_store(backend)
    first.provision_secret("secret-a", 1, RAW_SECRET)
    old_handle = issue(first)

    restarted = new_store(backend)

    assert restarted.contains("secret-a", 1)
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        restarted.validate_handle(
            handle_ref=old_handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )


def test_missing_material_after_handle_issue_fails_closed_and_invalidates_handle() -> None:
    store, backend = seeded_store()
    handle = issue(store)
    backend.passwords.clear()

    with pytest.raises(ProtectedCredentialStoreError, match="unavailable"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ProtectedCredentialStoreError, match="unknown or invalidated"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=NOW + timedelta(seconds=2),
        )


def test_naive_expiry_and_use_timestamps_are_rejected() -> None:
    store, _backend = seeded_store()

    with pytest.raises(ProtectedCredentialStoreError, match="timezone-aware"):
        store.issue_handle(
            secret_ref="secret-a",
            generation=1,
            project_id=PROJECT,
            audience=AUDIENCE,
            scopes=frozenset({SCOPE}),
            expires_at=datetime(2026, 8, 20, 12, 5),
        )

    handle = issue(store)
    with pytest.raises(ProtectedCredentialStoreError, match="timezone-aware"):
        store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT,
            audience=AUDIENCE,
            scope=SCOPE,
            now=datetime(2026, 8, 20, 12, 1),
        )


def test_invalid_target_identity_is_rejected_before_backend_access() -> None:
    store = new_store()

    with pytest.raises(ProtectedCredentialStoreError, match="secret_ref"):
        store.contains(" ", 1)
    with pytest.raises(ProtectedCredentialStoreError, match="generation"):
        store.contains("secret-a", 0)


def test_invalid_service_prefix_is_rejected() -> None:
    with pytest.raises(ProtectedCredentialStoreError, match="prefix"):
        WindowsCredentialStore(FakeWinVaultBackend(), service_prefix="\x00")


def test_factory_fails_clearly_off_windows_without_backend_autoselection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_credentials.sys, "platform", "linux")

    with pytest.raises(ProtectedCredentialStoreError, match="requires Windows"):
        windows_credentials.create_windows_credential_store()


def _install_fake_keyring_modules(
    monkeypatch: pytest.MonkeyPatch,
    backend_type: type[object],
) -> None:
    keyring_module = types.ModuleType("keyring")
    keyring_module.__path__ = []  # type: ignore[attr-defined]
    backends_module = types.ModuleType("keyring.backends")
    backends_module.__path__ = []  # type: ignore[attr-defined]
    windows_module = types.ModuleType("keyring.backends.Windows")
    windows_module.WinVaultKeyring = backend_type  # type: ignore[attr-defined]
    keyring_module.backends = backends_module  # type: ignore[attr-defined]
    backends_module.Windows = windows_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends_module)
    monkeypatch.setitem(sys.modules, "keyring.backends.Windows", windows_module)


def test_factory_uses_explicit_windows_backend_and_local_machine_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWinVaultKeyring(FakeWinVaultBackend):
        priority = 5.0

    _install_fake_keyring_modules(monkeypatch, FakeWinVaultKeyring)
    monkeypatch.setattr(windows_credentials.sys, "platform", "win32")

    store = windows_credentials.create_windows_credential_store()

    assert isinstance(store._backend, FakeWinVaultKeyring)
    assert store._backend.persist == "local machine"


def test_factory_backend_initialization_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenWinVaultKeyring:
        priority = 5.0

        def __init__(self) -> None:
            raise RuntimeError(RAW_SECRET)

    _install_fake_keyring_modules(monkeypatch, BrokenWinVaultKeyring)
    monkeypatch.setattr(windows_credentials.sys, "platform", "win32")

    with pytest.raises(ProtectedCredentialStoreError) as captured:
        windows_credentials.create_windows_credential_store()

    assert RAW_SECRET not in str(captured.value)
    assert "RuntimeError" in str(captured.value)
