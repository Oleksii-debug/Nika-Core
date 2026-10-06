from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, runtime_checkable


class IsolationClass(StrEnum):
    POLICY_ONLY = "policy_only"
    PROCESS_CONTAINED = "process_contained"
    OS_SANDBOXED = "os_sandboxed"
    REMOTE_SANDBOXED = "remote_sandboxed"


class NetworkMode(StrEnum):
    DENY = "deny"
    APPROVED_HOSTS = "approved_hosts"


class GapKind(StrEnum):
    MISSING_CAPABILITY = "missing_capability"
    EXISTING_CAPABILITY_AVAILABLE = "existing_capability_available"
    MISSING_INFORMATION = "missing_information"
    AMBIGUOUS_GOAL = "ambiguous_goal"
    TOOL_FAILED = "tool_failed"
    MODEL_FAILED = "model_failed"
    PERMISSION_DENIED = "permission_denied"


class GapDisposition(StrEnum):
    REUSE = "reuse"
    BUILD = "build"
    BLOCK = "block"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    REUSE_SELECTED = "reuse_selected"
    BUILD_REQUIRED = "build_required"
    BUILDING = "building"
    BUILT = "built"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REGISTERING = "registering"
    REGISTERED = "registered"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"
    ROLLED_BACK = "rolled_back"


TERMINAL_CANDIDATE_STATES = frozenset(
    {
        CandidateState.REGISTERED,
        CandidateState.REJECTED,
        CandidateState.BLOCKED,
        CandidateState.QUARANTINED,
        CandidateState.ROLLED_BACK,
    }
)


class WorkerFailureKind(StrEnum):
    INVALID_REQUEST = "invalid_request"
    POLICY_VIOLATION = "policy_violation"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROCESS_FAILED = "process_failed"
    INTERNAL_ERROR = "internal_error"


def normalize_relative_path(value: str) -> PurePosixPath:
    stripped = value.strip()
    win = PureWindowsPath(stripped)
    posix = PurePosixPath(stripped.replace("\\", "/"))
    lowered_parts = tuple(part.casefold() for part in posix.parts)
    if (
        not stripped
        or posix == PurePosixPath(".")
        or win.drive
        or posix.is_absolute()
        or ".." in posix.parts
        or ".git" in lowered_parts
        or any(":" in part for part in posix.parts)
    ):
        raise ValueError("path must stay inside repository-relative non-.git scope")
    return posix


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    repository_id: str
    base_sha: str
    tree_digest: str

    def __post_init__(self) -> None:
        if not self.repository_id.strip():
            raise ValueError("repository_id must not be empty")
        if len(self.base_sha) != 40 or any(c not in "0123456789abcdef" for c in self.base_sha.lower()):
            raise ValueError("base_sha must be a 40-character hexadecimal SHA")
        if not self.tree_digest.strip():
            raise ValueError("tree_digest must not be empty")


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    lease_id: str
    workspace_root: Path
    isolation_class: IsolationClass
    expires_at: str

    def __post_init__(self) -> None:
        if not self.lease_id.strip():
            raise ValueError("lease_id must not be empty")
        if not str(self.workspace_root):
            raise ValueError("workspace_root must not be empty")
        if not self.expires_at.strip():
            raise ValueError("expires_at must not be empty")


@dataclass(frozen=True, slots=True)
class AllowedPathPolicy:
    roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("at least one allowed path root is required")
        for root in self.roots:
            normalize_relative_path(root)

    def allows(self, value: str) -> bool:
        candidate = normalize_relative_path(value)
        roots = tuple(normalize_relative_path(root) for root in self.roots)
        return any(candidate == root or root in candidate.parents for root in roots)


@dataclass(frozen=True, slots=True)
class ProcessPolicy:
    allowed_executables: tuple[str, ...]
    shell_allowed: bool = False

    def __post_init__(self) -> None:
        if self.shell_allowed:
            raise ValueError("generic shell execution is not allowed")
        if not self.allowed_executables:
            raise ValueError("at least one executable must be allowlisted")
        if any(not item.strip() for item in self.allowed_executables):
            raise ValueError("allowed executable names must not be empty")


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    mode: NetworkMode = NetworkMode.DENY
    approved_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is NetworkMode.DENY and self.approved_hosts:
            raise ValueError("DENY network policy cannot contain approved hosts")
        if self.mode is NetworkMode.APPROVED_HOSTS and not self.approved_hosts:
            raise ValueError("approved-host network policy requires at least one host")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    timeout_seconds: int
    max_output_bytes: int
    max_changed_files: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0 or self.max_changed_files <= 0:
            raise ValueError("resource budget values must be positive")


@dataclass(frozen=True, slots=True)
class AcceptanceCommand:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not item for item in self.argv):
            raise ValueError("acceptance command argv must not be empty")
        if self.argv[0].casefold() in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh"}:
            raise ValueError("shell executables are not valid acceptance command entrypoints")
        if self.cwd != ".":
            normalize_relative_path(self.cwd)
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("acceptance command timeout must be positive")


@dataclass(frozen=True, slots=True)
class CodingJob:
    job_id: str
    task_id: str
    goal: str
    repository: RepositorySnapshot
    lease: WorkspaceLease
    allowed_paths: AllowedPathPolicy
    process_policy: ProcessPolicy
    network_policy: NetworkPolicy
    resource_budget: ResourceBudget
    acceptance_commands: tuple[AcceptanceCommand, ...]
    permission_ceiling: frozenset[str]

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.task_id.strip() or not self.goal.strip():
            raise ValueError("coding job identity and goal must not be empty")
        if not self.permission_ceiling:
            raise ValueError("coding job permission ceiling must not be empty")


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        normalize_relative_path(self.path)
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256.lower()):
            raise ValueError("changed-file sha256 must be hexadecimal")
        if self.size_bytes < 0:
            raise ValueError("changed-file size must be non-negative")


@dataclass(frozen=True, slots=True)
class TestEvidence:
    command: tuple[str, ...]
    exit_code: int
    output_digest: str

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("test evidence requires a command")
        if not self.output_digest.strip():
            raise ValueError("test evidence requires an output digest")


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    name: str
    digest: str
    media_type: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.digest.strip() or not self.media_type.strip():
            raise ValueError("artifact evidence fields must not be empty")


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    kind: WorkerFailureKind
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("worker failure message must not be empty")


@dataclass(frozen=True, slots=True)
class RecoveryState:
    phase: str
    opaque_token: str | None = None

    def __post_init__(self) -> None:
        if not self.phase.strip():
            raise ValueError("recovery phase must not be empty")


@dataclass(frozen=True, slots=True)
class CodingResult:
    job_id: str
    changed_files: tuple[ChangedFile, ...] = ()
    test_evidence: tuple[TestEvidence, ...] = ()
    artifacts: tuple[ArtifactEvidence, ...] = ()
    recovery_state: RecoveryState | None = None
    failure: WorkerFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    task_id: str
    requested_capability: str
    kind: GapKind
    reason: str
    attempted_methods: tuple[str, ...] = ()
    permission_ceiling: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.requested_capability.strip() or not self.reason.strip():
            raise ValueError("capability gap identity and reason must not be empty")


@dataclass(frozen=True, slots=True)
class ReuseCandidate:
    capability_id: str
    version: str
    source: str
    digest: str
    permissions: frozenset[str]
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.capability_id, self.version, self.source, self.digest)):
            raise ValueError("reuse candidate identity must not be empty")


@dataclass(frozen=True, slots=True)
class CapabilityManifestV1:
    capability_id: str
    version: str
    digest: str
    entrypoint: str
    permissions: frozenset[str]
    source: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("only capability manifest schema v1 is supported")
        if not all(value.strip() for value in (self.capability_id, self.version, self.digest, self.entrypoint, self.source)):
            raise ValueError("capability manifest fields must not be empty")
        if not self.permissions:
            raise ValueError("capability manifest must declare permissions")


@dataclass(frozen=True, slots=True)
class GapDecision:
    disposition: GapDisposition
    reason: str


@runtime_checkable
class CodingWorkerPort(Protocol):
    async def execute(self, job: CodingJob) -> CodingResult: ...

    async def cancel(self, job_id: str) -> None: ...

    async def inspect(self, job_id: str) -> RecoveryState | None: ...

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult: ...
