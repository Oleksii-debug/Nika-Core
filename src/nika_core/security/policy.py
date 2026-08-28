from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from nika_core.tools import ToolRisk

V01_APPROVAL_AUTHORITY_VERSION = "nika-v01-approval-v1"
_EFFECT_INTENT_SCHEMA = "nika-v01-effect-intent-v1"
_APPROVAL_INTENT_SCHEMA = "nika-v01-approval-intent-v1"

_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_WINDOWS_INVALID_COMPONENT_CHARS = frozenset('<>:"|?*')


def _validate_windows_component(component: str, *, label: str) -> None:
    if not component:
        raise ValueError(f"{label} contains an empty path component")
    if component != component.rstrip(" ."):
        raise ValueError(f"{label} contains a trailing Windows space or period")
    if any(char in _WINDOWS_INVALID_COMPONENT_CHARS for char in component):
        raise ValueError(f"{label} contains a Windows-reserved character")
    if any(ord(char) < 32 for char in component):
        raise ValueError(f"{label} contains a Windows control character")
    device_stem = component.split(".", 1)[0].casefold()
    if device_stem in _WINDOWS_RESERVED_DEVICE_NAMES:
        raise ValueError(f"{label} contains a Windows-reserved device name")


def _normalize_workspace_relative(value: str, *, label: str) -> PurePosixPath:
    if not value or not value.strip():
        raise ValueError(f"{label} must stay inside a workspace-relative scope")
    windows_path = PureWindowsPath(value)
    normalized = PurePosixPath(value.replace("\\", "/"))
    if (
        normalized == PurePosixPath(".")
        or windows_path.drive
        or windows_path.root
        or normalized.is_absolute()
        or ".." in normalized.parts
    ):
        raise ValueError(f"{label} must stay inside a workspace-relative scope")
    for component in normalized.parts:
        _validate_windows_component(component, label=label)
        if component.casefold() == ".git":
            raise ValueError(f"{label} cannot target .git metadata")
    return normalized


def _executable_scope(value: str) -> tuple[str, str, str]:
    """Return (scope kind, normalized identity, case-folded basename)."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("process executable must not be empty")
    if stripped != value:
        raise ValueError("process executable must not contain surrounding whitespace")

    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    path_scoped = bool(
        windows_path.drive
        or windows_path.root
        or posix_path.is_absolute()
        or "/" in value
        or "\\" in value
    )
    if not path_scoped:
        _validate_windows_component(value, label="process executable")
        normalized_name = value.casefold()
        return ("name", normalized_name, normalized_name)

    if windows_path.drive or "\\" in value or value.startswith("//"):
        drive = windows_path.drive.casefold()
        if drive.startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("Win32 device namespace executables are not allowed")
        if not windows_path.is_absolute():
            raise ValueError("Windows executable path scope must be absolute")
        if not windows_path.name:
            raise ValueError("process executable path must identify a file")
        for component in windows_path.parts[1:]:
            _validate_windows_component(component, label="process executable")
        return (
            "windows-path",
            windows_path.as_posix().casefold(),
            windows_path.name.casefold(),
        )

    if not posix_path.is_absolute():
        raise ValueError("POSIX executable path scope must be absolute")
    basename = posix_path.name
    if not basename:
        raise ValueError("process executable path must identify a file")
    return ("posix-path", posix_path.as_posix(), basename.casefold())


def _normalize_text(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    return unicodedata.normalize("NFC", value)


def _normalize_optional_text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, label=label)


def _normalize_host(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    normalized = _normalize_text(value, label=label).casefold().rstrip(".")
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _normalize_json_value(value: object, *, path: str = "arguments") -> object:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"{path} keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise ValueError(f"{path} contains duplicate normalized key {key!r}")
            normalized[key] = _normalize_json_value(raw_value, path=f"{path}.{key}")
        return normalized
    raise ValueError(f"{path} contains unsupported value type {type(value).__name__}")


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _canonical_arguments(arguments: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    normalized = _normalize_json_value(arguments)
    if not isinstance(normalized, dict):
        raise ValueError("arguments must be a mapping")
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    frozen = _freeze_json_value(normalized)
    assert isinstance(frozen, Mapping)
    return encoded, frozen


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    workspace_root: Path
    writable_roots: tuple[str, ...] = ()
    allowed_network_hosts: tuple[str, ...] = ()
    allowed_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        object.__setattr__(self, "workspace_root", root)
        normalized_roots = tuple(
            _normalize_workspace_relative(relative, label="writable root").as_posix()
            for relative in self.writable_roots
        )
        object.__setattr__(self, "writable_roots", normalized_roots)
        for executable in self.allowed_executables:
            _executable_scope(executable)

    def resolve_write(self, relative_path: str) -> Path:
        try:
            normalized = _normalize_workspace_relative(relative_path, label="write path")
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        candidate_path = Path(*normalized.parts)
        candidate = (self.workspace_root / candidate_path).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise PermissionError("write path escapes workspace")
        if not self.writable_roots:
            raise PermissionError("filesystem writes are disabled")
        allowed = tuple((self.workspace_root / item).resolve() for item in self.writable_roots)
        if not any(candidate == root or root in candidate.parents for root in allowed):
            raise PermissionError("write path is outside allowed roots")
        return candidate

    def authorize_network(self, host: str) -> None:
        normalized = host.strip().lower().rstrip(".")
        allowed = {item.strip().lower().rstrip(".") for item in self.allowed_network_hosts}
        if not normalized or normalized not in allowed:
            raise PermissionError("network host is not allowed")

    def authorize_executable(self, executable: str) -> None:
        try:
            requested_kind, requested_identity, requested_name = _executable_scope(executable)
        except ValueError as exc:
            raise PermissionError("process executable is not allowed") from exc

        for allowed_executable in self.allowed_executables:
            allowed_kind, allowed_identity, _ = _executable_scope(allowed_executable)
            if allowed_kind == "name" and requested_name == allowed_identity:
                return
            if (
                allowed_kind != "name"
                and requested_kind == allowed_kind
                and requested_identity == allowed_identity
            ):
                return
        raise PermissionError("process executable is not allowed")


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_write_bytes: int = 0
    max_network_calls: int = 0
    max_process_launches: int = 0

    def __post_init__(self) -> None:
        if min(self.max_write_bytes, self.max_network_calls, self.max_process_launches) < 0:
            raise ValueError("execution budgets must be non-negative")


@dataclass(slots=True)
class ExecutionBudgetLedger:
    budget: ExecutionBudget
    write_bytes: int = 0
    network_calls: int = 0
    process_launches: int = 0
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def _next_usage_unlocked(self, intent: ActionIntent) -> tuple[int, int, int]:
        next_write = self.write_bytes + intent.write_bytes
        next_network = self.network_calls + int(intent.network_host is not None)
        next_process = self.process_launches + int(intent.executable is not None)
        if next_write > self.budget.max_write_bytes:
            raise PermissionError("filesystem write budget exceeded")
        if next_network > self.budget.max_network_calls:
            raise PermissionError("network call budget exceeded")
        if next_process > self.budget.max_process_launches:
            raise PermissionError("process launch budget exceeded")
        return next_write, next_network, next_process

    def _commit_usage_unlocked(self, usage: tuple[int, int, int]) -> None:
        self.write_bytes, self.network_calls, self.process_launches = usage

    def reserve(self, intent: ActionIntent) -> None:
        with self._lock:
            self._commit_usage_unlocked(self._next_usage_unlocked(intent))


@dataclass(frozen=True, slots=True)
class ActionIntent:
    action_id: str
    tool_id: str
    risk: ToolRisk
    target: str
    write_path: str | None = None
    write_bytes: int = 0
    network_host: str | None = None
    executable: str | None = None
    approval_required: bool = False
    task_id: str | None = None
    project_id: str | None = None
    site: str | None = None
    resource: str | None = None
    arguments: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)
    effect_id: str | None = None
    authority_version: str = V01_APPROVAL_AUTHORITY_VERSION
    scope: tuple[tuple[str, str], ...] = ()
    _normalized_arguments_json: str = field(init=False, repr=False, compare=False)
    _executable_identity: str | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _normalize_text(self.action_id, label="action_id"))
        object.__setattr__(self, "tool_id", _normalize_text(self.tool_id, label="tool_id"))
        object.__setattr__(self, "target", _normalize_text(self.target, label="target"))
        object.__setattr__(
            self,
            "task_id",
            _normalize_optional_text(self.task_id, label="task_id"),
        )
        object.__setattr__(
            self,
            "project_id",
            _normalize_optional_text(self.project_id, label="project_id"),
        )
        object.__setattr__(self, "site", _normalize_host(self.site, label="site"))
        object.__setattr__(
            self,
            "resource",
            _normalize_optional_text(self.resource, label="resource"),
        )
        object.__setattr__(
            self,
            "effect_id",
            _normalize_optional_text(self.effect_id, label="effect_id"),
        )
        object.__setattr__(
            self,
            "authority_version",
            _normalize_text(self.authority_version, label="authority_version"),
        )
        if self.write_bytes < 0:
            raise ValueError("write_bytes must be non-negative")
        if self.write_bytes and self.write_path is None:
            raise ValueError("write_bytes require write_path")
        if self.write_path is not None:
            object.__setattr__(
                self,
                "write_path",
                _normalize_workspace_relative(self.write_path, label="write path").as_posix(),
            )
        object.__setattr__(
            self,
            "network_host",
            _normalize_host(self.network_host, label="network_host"),
        )
        executable_identity: str | None = None
        if self.executable is not None:
            kind, identity, _ = _executable_scope(self.executable)
            executable_identity = f"{kind}:{identity}"
        object.__setattr__(self, "_executable_identity", executable_identity)

        normalized_scope: list[tuple[str, str]] = []
        seen_keys: set[str] = set()
        for raw_key, raw_value in self.scope:
            key = _normalize_text(raw_key, label="scope key")
            value = _normalize_text(raw_value, label=f"scope value for {key}")
            if key in seen_keys:
                raise ValueError("scope keys must be unique")
            seen_keys.add(key)
            normalized_scope.append((key, value))
        normalized_scope.sort(key=lambda item: item[0])
        object.__setattr__(self, "scope", tuple(normalized_scope))

        try:
            normalized_arguments, frozen_arguments = _canonical_arguments(self.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("arguments must be deterministic JSON-compatible data") from exc
        object.__setattr__(self, "arguments", frozen_arguments)
        object.__setattr__(self, "_normalized_arguments_json", normalized_arguments)

    @property
    def requires_approval(self) -> bool:
        return self.approval_required or self.risk in {
            ToolRisk.EXTERNAL_SIDE_EFFECT,
            ToolRisk.HIGH_IMPACT,
        }

    @property
    def normalized_arguments_json(self) -> str:
        return self._normalized_arguments_json

    @property
    def arguments_fingerprint(self) -> str:
        return _sha256_text(self._normalized_arguments_json)

    @property
    def effect_fingerprint(self) -> str:
        payload = {
            "action_id": self.action_id,
            "arguments": json.loads(self._normalized_arguments_json),
            "effect_id": self.effect_id,
            "executable": self._executable_identity,
            "network_host": self.network_host,
            "project_id": self.project_id,
            "resource": self.resource,
            "schema": _EFFECT_INTENT_SCHEMA,
            "site": self.site,
            "target": self.target,
            "task_id": self.task_id,
            "tool_id": self.tool_id,
            "write_bytes": self.write_bytes,
            "write_path": self.write_path,
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256_text(encoded)

    @property
    def approval_fingerprint(self) -> str:
        payload = {
            "approval_required": self.approval_required,
            "authority_version": self.authority_version,
            "effect_fingerprint": self.effect_fingerprint,
            "risk": self.risk.value,
            "schema": _APPROVAL_INTENT_SCHEMA,
            "scope": [list(item) for item in self.scope],
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256_text(encoded)


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approval_id: str
    request_id: str
    issuer_id: str
    authority_version: str
    action_fingerprint: str
    effect_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.approval_id, "approval_id"),
            (self.request_id, "request_id"),
            (self.issuer_id, "issuer_id"),
            (self.authority_version, "authority_version"),
            (self.action_fingerprint, "action_fingerprint"),
            (self.effect_fingerprint, "effect_fingerprint"),
            (self.signature, "signature"),
        ):
            _normalize_text(value, label=label)
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")


class ApprovalVerifier(Protocol):
    @property
    def authorization_lock(self) -> RLock: ...

    def validate_locked(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence,
        *,
        now: datetime,
    ) -> None: ...

    def commit_locked(self, approval: ApprovalEvidence) -> None: ...


@dataclass(slots=True)
class ApprovalLedger:
    _used: set[tuple[str, str]] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def _validate_unlocked(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence | None,
        *,
        now: datetime,
    ) -> ApprovalEvidence:
        if approval is None:
            raise PermissionError("explicit approval is required")
        if now.tzinfo is None:
            raise ValueError("current time must be timezone-aware")
        approval_key = (approval.issuer_id, approval.approval_id)
        if approval_key in self._used:
            raise PermissionError("approval evidence was already used")
        if approval.action_fingerprint != intent.approval_fingerprint:
            raise PermissionError("approval does not match the exact action")
        if approval.effect_fingerprint != intent.effect_fingerprint:
            raise PermissionError("approval does not match the exact effect")
        if approval.authority_version != intent.authority_version:
            raise PermissionError("approval authority version does not match")
        if now < approval.approved_at or now >= approval.expires_at:
            raise PermissionError("approval is not currently valid")
        return approval

    def _mark_used_unlocked(self, approval: ApprovalEvidence) -> None:
        self._used.add((approval.issuer_id, approval.approval_id))

    def consume(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence | None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Record local exactness/replay only; this does not authenticate evidence."""
        current = now or datetime.now(UTC)
        with self._lock:
            validated = self._validate_unlocked(intent, approval, now=current)
            self._mark_used_unlocked(validated)


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    granted_tools: frozenset[str]
    sandbox: SandboxPolicy
    budget: ExecutionBudget
    approval_verifier: ApprovalVerifier | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    action_id: str
    approved: bool
    resolved_write_path: Path | None = None


def authorize_action(
    intent: ActionIntent,
    policy: SecurityPolicy,
    budgets: ExecutionBudgetLedger,
    approvals: ApprovalLedger,
    *,
    approval: ApprovalEvidence | None = None,
    now: datetime | None = None,
) -> SecurityDecision:
    """Defense-in-depth authorization before an action reaches an external adapter."""
    if intent.tool_id not in policy.granted_tools:
        raise PermissionError("tool is not granted by downstream policy")

    resolved_write: Path | None = None
    if intent.write_path is not None:
        resolved_write = policy.sandbox.resolve_write(intent.write_path)
    if intent.network_host is not None:
        policy.sandbox.authorize_network(intent.network_host)
    if intent.executable is not None:
        policy.sandbox.authorize_executable(intent.executable)

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("current time must be timezone-aware")

    if intent.requires_approval:
        for value, label in (
            (intent.task_id, "task_id"),
            (intent.project_id, "project_id"),
            (intent.effect_id, "effect_id"),
        ):
            if value is None:
                raise PermissionError(f"approval-gated action requires {label}")
        verifier = policy.approval_verifier
        if verifier is None:
            raise PermissionError("trusted approval verifier is required")
        if approval is None:
            raise PermissionError("explicit approval is required")
        # Fixed lock order: trusted authority -> local replay ledger -> resource budget.
        # Validation is non-mutating. All local authority/budget state commits only after every
        # exact-effect, authenticity, expiry and budget check succeeds.
        with verifier.authorization_lock, approvals._lock, budgets._lock:
            next_usage = budgets._next_usage_unlocked(intent)
            verifier.validate_locked(intent, approval, now=current)
            validated = approvals._validate_unlocked(intent, approval, now=current)
            budgets._commit_usage_unlocked(next_usage)
            approvals._mark_used_unlocked(validated)
            verifier.commit_locked(validated)
    else:
        budgets.reserve(intent)

    return SecurityDecision(
        action_id=intent.action_id,
        approved=True,
        resolved_write_path=resolved_write,
    )
