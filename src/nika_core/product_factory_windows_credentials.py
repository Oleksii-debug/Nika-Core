from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol

_CREDENTIAL_BLOB_MAX_BYTES = 5 * 512
_GENERIC_TARGET_MAX_CHARS = 32767
_SERVICE_PREFIX = "NikaCore.ProductFactory.v1"
_USERNAME = "nika-core"


class ProtectedCredentialStoreError(RuntimeError):
    """Raised when the Windows protected credential-store boundary fails closed."""


class WindowsVaultBackendPort(Protocol):
    """Minimal python-keyring Windows backend surface used by Nika."""

    persist: object

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CredentialHandleUse:
    secret_ref: str
    generation: int
    project_id: str
    audience: str
    scope: str
    used_at: datetime

    def __post_init__(self) -> None:
        _aware(self.used_at)
        if self.generation < 1:
            raise ProtectedCredentialStoreError("credential generation must be positive")
        _nonempty("secret_ref", self.secret_ref)
        _nonempty("project_id", self.project_id)
        _nonempty("audience", self.audience)
        _nonempty("scope", self.scope)


@dataclass(frozen=True, slots=True)
class _HandleBinding:
    secret_ref: str
    generation: int
    project_id: str
    audience: str
    scopes: frozenset[str]
    expires_at: datetime


@dataclass(slots=True)
class WindowsCredentialStore:
    """Windows Credential Manager adapter with in-process opaque lease handles."""

    _backend: WindowsVaultBackendPort = field(repr=False)
    service_prefix: str = _SERVICE_PREFIX
    _handles: dict[str, _HandleBinding] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        _nonempty("credential service prefix", self.service_prefix)

    def provision_secret(self, secret_ref: str, generation: int, raw_secret: str) -> None:
        target = self._target(secret_ref, generation)
        material = _validated_material(raw_secret)
        material_bytes = _material_bytes(material)
        with self._lock:
            existing = self._read_password(target)
            if existing is None:
                self._set_password(target, material)
                return
            existing_bytes = _material_bytes(_validated_material(existing))
            if hmac.compare_digest(existing_bytes, material_bytes):
                return
        raise ProtectedCredentialStoreError(
            "credential generation already exists with different material; rotate generation"
        )

    def contains(self, secret_ref: str, generation: int) -> bool:
        target = self._target(secret_ref, generation)
        with self._lock:
            return self._read_password(target) is not None

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
        self._target(secret_ref, generation)
        _nonempty("project_id", project_id)
        _nonempty("audience", audience)
        if not scopes or any(not scope.strip() for scope in scopes):
            raise ProtectedCredentialStoreError("credential handle scopes must not be empty")
        instant = _aware(expires_at)
        with self._lock:
            if not self.contains(secret_ref, generation):
                raise ProtectedCredentialStoreError(
                    "protected credential generation is unavailable"
                )
            handle = "nika-credential-handle-" + secrets.token_urlsafe(32)
            self._handles[handle] = _HandleBinding(
                secret_ref,
                generation,
                project_id,
                audience,
                scopes,
                instant,
            )
            return handle

    def validate_handle(
        self,
        *,
        handle_ref: str,
        project_id: str,
        audience: str,
        scope: str,
        now: datetime | None = None,
    ) -> CredentialHandleUse:
        _nonempty("handle_ref", handle_ref)
        _nonempty("project_id", project_id)
        _nonempty("audience", audience)
        _nonempty("scope", scope)
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            binding = self._handles.get(handle_ref)
            if binding is None:
                raise ProtectedCredentialStoreError("unknown or invalidated credential handle")
            if binding.expires_at <= instant:
                self._handles.pop(handle_ref, None)
                raise ProtectedCredentialStoreError("credential handle has expired")
            if binding.project_id != project_id:
                raise ProtectedCredentialStoreError("credential handle belongs to another project")
            if binding.audience != audience:
                raise ProtectedCredentialStoreError("credential handle audience does not match")
            if scope not in binding.scopes:
                raise ProtectedCredentialStoreError(
                    "credential handle does not authorize requested scope"
                )
            if not self.contains(binding.secret_ref, binding.generation):
                self._handles.pop(handle_ref, None)
                raise ProtectedCredentialStoreError(
                    "protected credential generation is unavailable"
                )
            return CredentialHandleUse(
                binding.secret_ref,
                binding.generation,
                project_id,
                audience,
                scope,
                instant,
            )

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        self._target(secret_ref, generation)
        with self._lock:
            for handle_ref in [
                handle_ref
                for handle_ref, binding in self._handles.items()
                if binding.secret_ref == secret_ref and binding.generation == generation
            ]:
                del self._handles[handle_ref]

    def delete_secret(self, secret_ref: str, generation: int) -> bool:
        target = self._target(secret_ref, generation)
        with self._lock:
            self.revoke_handles(secret_ref, generation)
            if self._read_password(target) is None:
                return False
            try:
                self._backend.delete_password(target, _USERNAME)
            except Exception as exc:
                raise _backend_error("delete", exc) from None
            return True

    def _target(self, secret_ref: str, generation: int) -> str:
        _nonempty("secret_ref", secret_ref)
        if generation < 1:
            raise ProtectedCredentialStoreError("credential generation must be positive")
        try:
            encoded_ref = secret_ref.encode("utf-8")
        except UnicodeEncodeError:
            raise ProtectedCredentialStoreError("secret_ref must be valid UTF-8 text") from None
        digest = hashlib.sha256(encoded_ref).hexdigest()
        target = f"{self.service_prefix}.{digest}.g{generation}"
        if len(target) > _GENERIC_TARGET_MAX_CHARS:
            raise ProtectedCredentialStoreError("credential target exceeds Windows target limit")
        return target

    def _read_password(self, target: str) -> str | None:
        try:
            value = self._backend.get_password(target, _USERNAME)
        except Exception as exc:
            raise _backend_error("read", exc) from None
        if value is not None and not isinstance(value, str):
            raise ProtectedCredentialStoreError("credential backend returned invalid material type")
        return value

    def _set_password(self, target: str, raw_secret: str) -> None:
        try:
            self._backend.set_password(target, _USERNAME, raw_secret)
        except Exception as exc:
            raise _backend_error("write", exc) from None


def create_windows_credential_store() -> WindowsCredentialStore:
    """Create the explicit python-keyring WinVault adapter; never use auto-selected backends."""

    if sys.platform != "win32":
        raise ProtectedCredentialStoreError("Windows credential store requires Windows")
    try:
        from keyring.backends.Windows import WinVaultKeyring

        _ = WinVaultKeyring.priority
        backend = WinVaultKeyring()
        backend.persist = "local machine"
    except Exception as exc:
        raise _backend_error("initialize", exc) from None
    return WindowsCredentialStore(backend)


def _validated_material(raw_secret: str) -> str:
    if not isinstance(raw_secret, str):
        raise TypeError("credential material must be text")
    if not raw_secret or "\x00" in raw_secret:
        raise ProtectedCredentialStoreError("credential material is empty or contains NUL")
    if len(_material_bytes(raw_secret)) > _CREDENTIAL_BLOB_MAX_BYTES:
        raise ProtectedCredentialStoreError("credential material exceeds Windows credential limit")
    return raw_secret


def _material_bytes(raw_secret: str) -> bytes:
    try:
        return raw_secret.encode("utf-16-le")
    except UnicodeEncodeError:
        raise ProtectedCredentialStoreError(
            "credential material must be valid Unicode text"
        ) from None


def _nonempty(label: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not value.strip() or "\x00" in value:
        raise ProtectedCredentialStoreError(f"{label} must not be empty or contain NUL")
    return value


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("credential timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtectedCredentialStoreError("credential timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _backend_error(operation: str, exc: Exception) -> ProtectedCredentialStoreError:
    return ProtectedCredentialStoreError(
        f"Windows credential backend {operation} failed ({type(exc).__name__})"
    )
