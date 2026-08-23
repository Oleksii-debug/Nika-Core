from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from typing import Protocol

from nika_core.tools import ToolRisk

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

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.tool_id.strip() or not self.target.strip():
            raise ValueError("action identity fields must not be empty")
        if self.write_bytes < 0:
            raise ValueError("write_bytes must be non-negative")
        if self.write_bytes and self.write_path is None:
            raise ValueError("write_bytes require write_path")

    @property
    def approval_fingerprint(self) -> str:
        payload = json.dumps(
            (
                "nika-action-intent-v1",
                self.action_id,
                self.tool_id,
                self.risk.value,
                self.target,
                self.write_path,
                self.write_bytes,
                self.network_host,
                self.executable,
                self.approval_required,
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approval_id: str
    request_id: str
    issuer_id: str
    action_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        identity = (self.approval_id, self.request_id, self.issuer_id, self.action_fingerprint)
        if any(not item.strip() for item in identity) or not self.signature.strip():
            raise ValueError("approval identity, provenance and signature must not be empty")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")


class ApprovalVerifier(Protocol):
    """Host-owned verifier participating in the same atomic commit as budget use."""

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

    def verify(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence,
        *,
        now: datetime,
    ) -> None: ...


@dataclass(slots=True)
class ApprovalLedger:
    _used: set[tuple[str, str]] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def _validate_unlocked(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence | None,
        *,
        now: datetime | None = None,
    ) -> ApprovalEvidence:
        if approval is None:
            raise PermissionError("explicit approval is required")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("current time must be timezone-aware")
        approval_key = (approval.issuer_id, approval.approval_id)
        if approval_key in self._used:
            raise PermissionError("approval evidence was already used")
        if approval.action_fingerprint != intent.approval_fingerprint:
            raise PermissionError("approval does not match the exact action")
        if current < approval.approved_at or current >= approval.expires_at:
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
        with self._lock:
            validated = self._validate_unlocked(intent, approval, now=now)
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

    requires_approval = intent.approval_required or intent.risk is ToolRisk.HIGH_IMPACT
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("current time must be timezone-aware")

    verifier = policy.approval_verifier
    if requires_approval:
        if verifier is None:
            raise PermissionError("trusted approval verifier is required")
        if approval is None:
            raise PermissionError("explicit approval is required")
        # Fixed global lock order: trusted host verifier -> local approval ledger -> budget.
        # Every check is non-mutating; host replay state, local replay state, and budget are
        # committed together only after all checks succeed.
        with verifier.authorization_lock, approvals._lock, budgets._lock:
            next_usage = budgets._next_usage_unlocked(intent)
            verifier.validate_locked(intent, approval, now=current)
            validated_approval = approvals._validate_unlocked(intent, approval, now=current)
            budgets._commit_usage_unlocked(next_usage)
            approvals._mark_used_unlocked(validated_approval)
            verifier.commit_locked(validated_approval)
    else:
        with budgets._lock:
            next_usage = budgets._next_usage_unlocked(intent)
            budgets._commit_usage_unlocked(next_usage)

    return SecurityDecision(
        action_id=intent.action_id,
        approved=True,
        resolved_write_path=resolved_write,
    )
