from __future__ import annotations

import ctypes
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
_AUTHORITY_SEGMENT = "authority"
_ERROR_ALREADY_EXISTS = 183
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_PROCESS_AUTHORITY_LOCK_GUARD = RLock()
_PROCESS_AUTHORITY_HANDLES: dict[str, int] = {}


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
        _positive_generation(self.generation)
        _nonempty("secret_ref", self.secret_ref)
        _nonempty("project_id", self.project_id)
        _nonempty("audience", self.audience)
        _nonempty("scope", self.scope)


@dataclass(frozen=True, slots=True)
class _HandleBinding:
    operation_id: str
    secret_ref: str
    generation: int
    project_id: str
    audience: str
    scopes: frozenset[str]
    expires_at: datetime
    authority_fingerprint: str


@dataclass(slots=True)
class WindowsCredentialStore:
    """Windows Credential Manager adapter with process-owned opaque lease handles."""

    _backend: WindowsVaultBackendPort = field(repr=False)
    service_prefix: str = _SERVICE_PREFIX
    _handles: dict[str, _HandleBinding] = field(default_factory=dict, init=False, repr=False)
    _operation_handles: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        _nonempty("credential service prefix", self.service_prefix)
        _ensure_process_authority_owner(self.service_prefix)

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

    def bind_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> None:
        target = self._authority_target(secret_ref, generation)
        fingerprint = _fingerprint(authority_fingerprint)
        with self._lock:
            if not self.contains(secret_ref, generation):
                raise ProtectedCredentialStoreError(
                    "protected credential generation is unavailable for authority binding"
                )
            existing = self._read_password(target)
            if existing is None:
                self._set_password(target, fingerprint)
                return
            if hmac.compare_digest(_fingerprint(existing), fingerprint):
                return
        raise ProtectedCredentialStoreError(
            "credential authority binding conflicts with existing protected metadata"
        )

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool:
        target = self._authority_target(secret_ref, generation)
        fingerprint = _fingerprint(authority_fingerprint)
        with self._lock:
            existing = self._read_password(target)
            if existing is None:
                return False
            return hmac.compare_digest(_fingerprint(existing), fingerprint)

    def retire_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        current_authority_fingerprint: str,
        retired_authority_fingerprint: str,
    ) -> None:
        target = self._authority_target(secret_ref, generation)
        current = _fingerprint(current_authority_fingerprint)
        retired = _fingerprint(retired_authority_fingerprint)
        if hmac.compare_digest(current, retired):
            raise ProtectedCredentialStoreError(
                "credential authority retirement requires a state transition"
            )
        with self._lock:
            existing = self._read_password(target)
            if existing is None:
                raise ProtectedCredentialStoreError(
                    "credential authority binding is unavailable for retirement"
                )
            existing_fingerprint = _fingerprint(existing)
            if hmac.compare_digest(existing_fingerprint, retired):
                return
            if not hmac.compare_digest(existing_fingerprint, current):
                raise ProtectedCredentialStoreError(
                    "credential authority retirement conflicts with protected metadata"
                )
            self._set_password(target, retired)

    def issue_handle(
        self,
        *,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
        operation_id: str | None = None,
    ) -> str:
        effective_operation_id = operation_id or (
            "nika-store-operation-" + secrets.token_urlsafe(24)
        )
        with self._lock:
            authority_fingerprint = self._current_authority_fingerprint(
                secret_ref,
                generation,
            )
            binding = self._handle_binding(
                operation_id=effective_operation_id,
                secret_ref=secret_ref,
                generation=generation,
                project_id=project_id,
                audience=audience,
                scopes=scopes,
                expires_at=expires_at,
                authority_fingerprint=authority_fingerprint,
            )
            existing_handle = self._operation_handles.get(effective_operation_id)
            if existing_handle is not None:
                self._require_operation_binding(existing_handle, binding)
                return existing_handle
            if not self.contains(secret_ref, generation):
                raise ProtectedCredentialStoreError(
                    "protected credential generation is unavailable"
                )
            handle = "nika-credential-handle-" + secrets.token_urlsafe(32)
            self._handles[handle] = binding
            self._operation_handles[effective_operation_id] = handle
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
        with self._lock:
            handle = self._operation_handles.get(operation_id)
            if handle is None:
                return None
            authority_fingerprint = self._current_authority_fingerprint(
                secret_ref,
                generation,
            )
            binding = self._handle_binding(
                operation_id=operation_id,
                secret_ref=secret_ref,
                generation=generation,
                project_id=project_id,
                audience=audience,
                scopes=scopes,
                expires_at=expires_at,
                authority_fingerprint=authority_fingerprint,
            )
            self._require_operation_binding(handle, binding)
            if not self.contains(secret_ref, generation):
                self._drop_handle(handle)
                raise ProtectedCredentialStoreError(
                    "protected credential generation is unavailable"
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
                self._drop_handle(handle_ref)
                raise ProtectedCredentialStoreError("credential handle has expired")
            if binding.project_id != project_id:
                raise ProtectedCredentialStoreError("credential handle belongs to another project")
            if binding.audience != audience:
                raise ProtectedCredentialStoreError("credential handle audience does not match")
            if scope not in binding.scopes:
                raise ProtectedCredentialStoreError(
                    "credential handle does not authorize requested scope"
                )
            try:
                current_authority = self._current_authority_fingerprint(
                    binding.secret_ref,
                    binding.generation,
                )
            except ProtectedCredentialStoreError:
                self._drop_handle(handle_ref)
                raise
            if not hmac.compare_digest(current_authority, binding.authority_fingerprint):
                self._drop_handle(handle_ref)
                raise ProtectedCredentialStoreError(
                    "credential handle authority was retired or superseded"
                )
            if not self.contains(binding.secret_ref, binding.generation):
                self._drop_handle(handle_ref)
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
                self._drop_handle(handle_ref)

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

    def delete_authority(self, secret_ref: str, generation: int) -> bool:
        authority_target = self._authority_target(secret_ref, generation)
        with self._lock:
            if self.contains(secret_ref, generation):
                raise ProtectedCredentialStoreError(
                    "credential authority cannot be retired while secret material exists"
                )
            if self._read_password(authority_target) is None:
                return False
            try:
                self._backend.delete_password(authority_target, _USERNAME)
            except Exception as exc:
                raise _backend_error("delete authority", exc) from None
            return True

    def _handle_binding(
        self,
        *,
        operation_id: str,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
        authority_fingerprint: str,
    ) -> _HandleBinding:
        _nonempty("operation_id", operation_id)
        self._target(secret_ref, generation)
        _nonempty("project_id", project_id)
        _nonempty("audience", audience)
        if not scopes or any(not scope.strip() for scope in scopes):
            raise ProtectedCredentialStoreError("credential handle scopes must not be empty")
        return _HandleBinding(
            operation_id,
            secret_ref,
            generation,
            project_id,
            audience,
            scopes,
            _aware(expires_at),
            _fingerprint(authority_fingerprint),
        )

    def _current_authority_fingerprint(self, secret_ref: str, generation: int) -> str:
        target = self._authority_target(secret_ref, generation)
        existing = self._read_password(target)
        if existing is None:
            raise ProtectedCredentialStoreError(
                "protected credential authority binding is unavailable"
            )
        return _fingerprint(existing)

    def _require_operation_binding(
        self,
        handle_ref: str,
        expected: _HandleBinding,
    ) -> None:
        actual = self._handles.get(handle_ref)
        if actual is None:
            raise ProtectedCredentialStoreError(
                "credential handle operation index references missing handle"
            )
        if actual != expected:
            raise ProtectedCredentialStoreError(
                "credential handle operation identity conflicts with existing binding"
            )

    def _drop_handle(self, handle_ref: str) -> None:
        binding = self._handles.pop(handle_ref, None)
        if binding is not None and self._operation_handles.get(binding.operation_id) == handle_ref:
            del self._operation_handles[binding.operation_id]

    def _target(self, secret_ref: str, generation: int) -> str:
        digest = self._reference_digest(secret_ref, generation)
        return self._bounded_target(f"{self.service_prefix}.{digest}.g{generation}")

    def _authority_target(self, secret_ref: str, generation: int) -> str:
        digest = self._reference_digest(secret_ref, generation)
        return self._bounded_target(
            f"{self.service_prefix}.{_AUTHORITY_SEGMENT}.{digest}.g{generation}"
        )

    def _reference_digest(self, secret_ref: str, generation: int) -> str:
        _nonempty("secret_ref", secret_ref)
        _positive_generation(generation)
        try:
            encoded_ref = secret_ref.encode("utf-8")
        except UnicodeEncodeError:
            raise ProtectedCredentialStoreError("secret_ref must be valid UTF-8 text") from None
        return hashlib.sha256(encoded_ref).hexdigest()

    def _bounded_target(self, target: str) -> str:
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


def _current_user_scope_key() -> str:
    """Return a stable hash of the primary Windows user SID for cross-session namespacing."""

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        # Non-Windows unit tests can monkeypatch the Win32 event surface without
        # fabricating a real access token. Real Windows CPython always exposes windll.
        return "nonwindows-test-user"
    try:
        kernel32 = windll.kernel32
        advapi32 = windll.advapi32
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        open_process_token = advapi32.OpenProcessToken
        open_process_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        open_process_token.restype = ctypes.c_int
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_token_information.restype = ctypes.c_int
        get_length_sid = advapi32.GetLengthSid
        get_length_sid.argtypes = [ctypes.c_void_p]
        get_length_sid.restype = ctypes.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        token = ctypes.c_void_p()
        if not open_process_token(
            get_current_process(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise ProtectedCredentialStoreError(
                "credential authority user identity could not open process token"
            )
        try:
            required = ctypes.c_uint32(0)
            get_token_information(
                token,
                _TOKEN_USER,
                None,
                0,
                ctypes.byref(required),
            )
            if required.value == 0:
                raise ProtectedCredentialStoreError(
                    "credential authority user identity size is unavailable"
                )
            buffer = ctypes.create_string_buffer(required.value)
            if not get_token_information(
                token,
                _TOKEN_USER,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise ProtectedCredentialStoreError(
                    "credential authority user identity could not be read"
                )
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_pointer:
                raise ProtectedCredentialStoreError(
                    "credential authority user identity is invalid"
                )
            sid_length = int(get_length_sid(sid_pointer))
            if sid_length <= 0:
                raise ProtectedCredentialStoreError(
                    "credential authority user identity length is invalid"
                )
            sid_bytes = ctypes.string_at(sid_pointer, sid_length)
            return hashlib.sha256(sid_bytes).hexdigest()
        finally:
            close_handle(token)
    except ProtectedCredentialStoreError:
        raise
    except Exception as exc:
        raise ProtectedCredentialStoreError(
            f"credential authority user identity failed ({type(exc).__name__})"
        ) from None


def _ensure_process_authority_owner(service_prefix: str) -> None:
    if sys.platform != "win32":
        return
    # Preserve a narrow fake-Windows unit seam. Physical Windows always exposes
    # ctypes.WinDLL, and the real gate is exercised by the PF3 Windows proof.
    if not hasattr(ctypes, "WinDLL"):
        return
    try:
        encoded_prefix = service_prefix.encode("utf-8")
    except UnicodeEncodeError:
        raise ProtectedCredentialStoreError(
            "credential service prefix must be valid UTF-8 text"
        ) from None
    user_scope = _current_user_scope_key()
    lock_key = hashlib.sha256(encoded_prefix).hexdigest()
    process_key = f"{user_scope}:{lock_key}"
    with _PROCESS_AUTHORITY_LOCK_GUARD:
        if process_key in _PROCESS_AUTHORITY_HANDLES:
            return
        object_name = (
            "Global\\NikaCore.ProductFactory.CredentialAuthority."
            f"{user_scope}.{lock_key}"
        )
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_event = kernel32.CreateEventW
            create_event.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_wchar_p,
            ]
            create_event.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            ctypes.set_last_error(0)
            raw_handle = create_event(None, 0, 0, object_name)
            last_error = ctypes.get_last_error()
            if not raw_handle:
                raise ProtectedCredentialStoreError(
                    f"credential authority host initialization failed (winerror={last_error})"
                )
            handle = int(raw_handle)
            if last_error == _ERROR_ALREADY_EXISTS:
                close_handle(ctypes.c_void_p(handle))
                raise ProtectedCredentialStoreError(
                    "another credential authority host is active"
                )
        except ProtectedCredentialStoreError:
            raise
        except Exception as exc:
            raise ProtectedCredentialStoreError(
                f"credential authority host initialization failed ({type(exc).__name__})"
            ) from None
        _PROCESS_AUTHORITY_HANDLES[process_key] = handle


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


def _fingerprint(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("credential authority fingerprint must be text")
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ProtectedCredentialStoreError(
            "credential authority fingerprint must be canonical sha256"
        )
    return value


def _positive_generation(generation: int) -> int:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ProtectedCredentialStoreError("credential generation must be a positive integer")
    return generation


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
