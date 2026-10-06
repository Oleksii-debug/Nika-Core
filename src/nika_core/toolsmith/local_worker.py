from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import pathlib
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    ArtifactEvidence,
    ChangedFile,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    IsolationClass,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.execution import (
    _git,
    _resolve_host_git_executable,
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
    run_typed_process,
)
from nika_core.toolsmith.workspace_security import (
    TreeDeltaEvidence,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    assert_cleanup_tree_safe,
    collect_tree_delta_evidence,
    collect_tree_evidence,
    ensure_path_policy,
    ensure_real_directory_root,
    ensure_worker_mutation_path,
    make_sterile_git_plan,
    sterile_git_environment,
)

_STATE_SCHEMA = "nika-contained-local-worker-v1"
_MAX_EDIT_BYTES = 8 * 1024 * 1024
_MAX_PLAN_BYTES = 32 * 1024 * 1024
_MAX_STATE_BYTES = 1024 * 1024
_MAX_STATE_JSON_DEPTH = 64
_MAX_STATE_JSON_INTEGER_BITS = 4096
_MAX_STATE_JSON_INTEGER_DECIMAL_CHARS = 1234


class ContainedLocalWorkerError(RuntimeError):
    """Raised when local CodingWorker authority cannot be proven safely."""


class _JobExecutionLock:
    """Cross-instance/process single-flight lock for one deterministic job root."""

    def __init__(self, job_root: pathlib.Path) -> None:
        self._path = job_root / "_nika_execution.lock"
        self._fd: int | None = None

    @staticmethod
    def _is_reparse(file_stat: os.stat_result) -> bool:
        attributes = getattr(file_stat, "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(flag and attributes & flag)

    def acquire(self) -> bool:
        if self._fd is not None:
            raise ContainedLocalWorkerError("job execution lock is already acquired")
        try:
            existing = self._path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ContainedLocalWorkerError("unable to inspect job execution lock") from exc
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or self._is_reparse(existing)
        ):
            raise ContainedLocalWorkerError("job execution lock path is unsafe")

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ContainedLocalWorkerError("unable to open job execution lock") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or self._is_reparse(opened):
                raise ContainedLocalWorkerError("job execution lock descriptor is unsafe")
            if os.name == "nt":
                import msvcrt

                if opened.st_size < 1:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    os.close(fd)
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
            self._fd = fd
            return True
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class LocalFileEdit:
    """One exact replacement/addition inside the declared component scope."""

    path: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.path) is not str:
            raise ValueError("local edit path must be exact text")
        normalized = ensure_worker_mutation_path(self.path)
        if normalized.as_posix() != self.path:
            raise ValueError("local edit path must use canonical repository spelling")
        if type(self.content) is not bytes:
            raise ValueError("local edit content must be exact bytes")
        if len(self.content) > _MAX_EDIT_BYTES:
            raise ValueError("local edit exceeds per-file byte limit")


@dataclass(frozen=True, slots=True)
class LocalCodingPlan:
    """Bounded structured mutation plan; planning authority stays outside the worker."""

    edits: tuple[LocalFileEdit, ...]

    def __post_init__(self) -> None:
        if type(self.edits) is not tuple or not self.edits:
            raise ValueError("local coding plan requires at least one edit")
        if len(self.edits) > 10_000:
            raise ValueError("local coding plan has too many edits")
        seen: set[str] = set()
        total = 0
        for edit in self.edits:
            if type(edit) is not LocalFileEdit:
                raise ValueError("local coding plan contains an invalid edit carrier")
            edit.__post_init__()
            folded = edit.path.casefold()
            if folded in seen:
                raise ValueError("local coding plan repeats a case-insensitive path identity")
            seen.add(folded)
            total += len(edit.content)
            if total > _MAX_PLAN_BYTES:
                raise ValueError("local coding plan exceeds total byte limit")


class LocalCodingPlanPort(Protocol):
    """Model-neutral planning seam: deterministic, local-model, or external-model."""

    async def plan(self, job: CodingJob) -> LocalCodingPlan: ...


class LocalRepositoryExecutionAuthorityPort(Protocol):
    """Revalidate one repository root immediately at contained-local effect boundaries."""

    def require_repository_root(
        self,
        *,
        repository_id: str,
        root: pathlib.Path,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalExecutionEvidence:
    job_id: str
    repository_id: str
    base_sha: str
    result_sha: str
    diff_digest: str

    def __post_init__(self) -> None:
        _safe_text(self.job_id, "job_id")
        _safe_text(self.repository_id, "repository_id")
        for value, label in ((self.base_sha, "base_sha"), (self.result_sha, "result_sha")):
            if type(value) is not str or len(value) != 40:
                raise ValueError(f"{label} must be a 40-character Git SHA")
            if any(char not in "0123456789abcdef" for char in value.casefold()):
                raise ValueError(f"{label} must be a hexadecimal Git SHA")
        if type(self.diff_digest) is not str or len(self.diff_digest) != 64:
            raise ValueError("diff_digest must be a hexadecimal sha256")
        if any(char not in "0123456789abcdef" for char in self.diff_digest.casefold()):
            raise ValueError("diff_digest must be a hexadecimal sha256")


def _safe_text(
    value: object,
    label: str,
    *,
    max_bytes: int = 4096,
    allow_lines: bool = False,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty canonical text")
    allowed_controls = {"\n", "\r", "\t"} if allow_lines else set()
    if any(
        (ord(char) < 32 or ord(char) == 127) and char not in allowed_controls
        for char in value
    ):
        raise ValueError(f"{label} contains control data")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the UTF-8 byte limit")
    return value


def _snapshot_job(job: CodingJob) -> CodingJob:
    if type(job) is not CodingJob:
        raise ValueError("coding job carrier is invalid")
    if type(job.repository) is not RepositorySnapshot:
        raise ValueError("repository snapshot carrier is invalid")
    if type(job.lease) is not WorkspaceLease:
        raise ValueError("workspace lease carrier is invalid")
    if type(job.allowed_paths) is not AllowedPathPolicy:
        raise ValueError("allowed path policy carrier is invalid")
    if type(job.process_policy) is not ProcessPolicy:
        raise ValueError("process policy carrier is invalid")
    if type(job.network_policy) is not NetworkPolicy:
        raise ValueError("network policy carrier is invalid")
    if type(job.resource_budget) is not ResourceBudget:
        raise ValueError("resource budget carrier is invalid")
    if type(job.lease.isolation_class) is not IsolationClass:
        raise ValueError("workspace isolation carrier is invalid")
    if type(job.network_policy.mode) is not NetworkMode:
        raise ValueError("network mode carrier is invalid")
    if type(job.acceptance_commands) is not tuple:
        raise ValueError("acceptance command collection is invalid")

    commands: list[AcceptanceCommand] = []
    for command in job.acceptance_commands:
        if type(command) is not AcceptanceCommand:
            raise ValueError("acceptance command carrier is invalid")
        commands.append(
            AcceptanceCommand(
                argv=tuple(command.argv),
                cwd=command.cwd,
                timeout_seconds=command.timeout_seconds,
            )
        )

    permissions = frozenset(job.permission_ceiling)
    if any(type(item) is not str for item in permissions):
        raise ValueError("permission ceiling contains non-text entries")

    return CodingJob(
        job_id=_safe_text(job.job_id, "job_id"),
        task_id=_safe_text(job.task_id, "task_id"),
        goal=_safe_text(job.goal, "goal", max_bytes=64 * 1024, allow_lines=True),
        repository=RepositorySnapshot(
            repository_id=_safe_text(job.repository.repository_id, "repository_id"),
            base_sha=job.repository.base_sha,
            tree_digest=_safe_text(job.repository.tree_digest, "repository tree digest"),
        ),
        lease=WorkspaceLease(
            lease_id=_safe_text(job.lease.lease_id, "lease_id"),
            workspace_root=pathlib.Path(job.lease.workspace_root),
            isolation_class=job.lease.isolation_class,
            expires_at=_safe_text(job.lease.expires_at, "lease expiry"),
        ),
        allowed_paths=AllowedPathPolicy(tuple(job.allowed_paths.roots)),
        process_policy=ProcessPolicy(
            tuple(job.process_policy.allowed_executables),
            shell_allowed=job.process_policy.shell_allowed,
        ),
        network_policy=NetworkPolicy(
            mode=job.network_policy.mode,
            approved_hosts=tuple(job.network_policy.approved_hosts),
        ),
        resource_budget=ResourceBudget(
            timeout_seconds=job.resource_budget.timeout_seconds,
            max_output_bytes=job.resource_budget.max_output_bytes,
            max_changed_files=job.resource_budget.max_changed_files,
        ),
        acceptance_commands=tuple(commands),
        permission_ceiling=permissions,
    )


def _snapshot_plan(plan: LocalCodingPlan, max_changed_files: int) -> LocalCodingPlan:
    if type(plan) is not LocalCodingPlan:
        raise ValueError("local coding planner returned an invalid plan carrier")
    copied = LocalCodingPlan(
        tuple(LocalFileEdit(edit.path, bytes(edit.content)) for edit in plan.edits)
    )
    if len(copied.edits) > max_changed_files:
        raise ValueError("local coding plan exceeds the job changed-file budget")
    return copied


def _expected_isolation() -> IsolationClass:
    return IsolationClass.PROCESS_CONTAINED if os.name == "nt" else IsolationClass.POLICY_ONLY


def _parse_expiry(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("workspace lease expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("workspace lease expiry must include a timezone")
    return parsed.astimezone(UTC)


def _job_fingerprint(job: CodingJob) -> str:
    payload = {
        "job_id": job.job_id,
        "task_id": job.task_id,
        "goal": job.goal,
        "repository_id": job.repository.repository_id,
        "base_sha": job.repository.base_sha.lower(),
        "tree_digest": job.repository.tree_digest,
        "lease_id": job.lease.lease_id,
        "workspace_root": str(job.lease.workspace_root),
        "isolation_class": job.lease.isolation_class.value,
        "allowed_paths": list(job.allowed_paths.roots),
        "allowed_executables": list(job.process_policy.allowed_executables),
        "network_mode": job.network_policy.mode.value,
        "approved_hosts": list(job.network_policy.approved_hosts),
        "resource_budget": [
            job.resource_budget.timeout_seconds,
            job.resource_budget.max_output_bytes,
            job.resource_budget.max_changed_files,
        ],
        "acceptance_commands": [
            [list(command.argv), command.cwd, command.timeout_seconds]
            for command in job.acceptance_commands
        ],
        "permission_ceiling": sorted(job.permission_ceiling),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _result_payload(result: CodingResult) -> dict[str, object]:
    return {
        "job_id": result.job_id,
        "changed_files": [
            {"path": item.path, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in result.changed_files
        ],
        "test_evidence": [
            {
                "command": list(item.command),
                "exit_code": item.exit_code,
                "output_digest": item.output_digest,
            }
            for item in result.test_evidence
        ],
        "artifacts": [
            {"name": item.name, "digest": item.digest, "media_type": item.media_type}
            for item in result.artifacts
        ],
        "recovery_state": (
            None
            if result.recovery_state is None
            else {
                "phase": result.recovery_state.phase,
                "opaque_token": result.recovery_state.opaque_token,
            }
        ),
        "failure": (
            None
            if result.failure is None
            else {
                "kind": result.failure.kind.value,
                "message": result.failure.message,
                "retryable": result.failure.retryable,
            }
        ),
    }


def _result_from_payload(payload: object) -> CodingResult:
    if type(payload) is not dict:
        raise ValueError("local worker result payload is invalid")
    if set(payload) != {
        "job_id",
        "changed_files",
        "test_evidence",
        "artifacts",
        "recovery_state",
        "failure",
    }:
        raise ValueError("local worker result payload has unexpected fields")

    changed_raw = payload["changed_files"]
    tests_raw = payload["test_evidence"]
    artifacts_raw = payload["artifacts"]
    if type(changed_raw) is not list or type(tests_raw) is not list:
        raise ValueError("local worker result collections are invalid")
    if type(artifacts_raw) is not list:
        raise ValueError("local worker artifact collection is invalid")

    changed: list[ChangedFile] = []
    for item in changed_raw:
        if type(item) is not dict or set(item) != {"path", "sha256", "size_bytes"}:
            raise ValueError("local worker changed-file payload is invalid")
        changed.append(
            ChangedFile(item["path"], item["sha256"], item["size_bytes"])
        )

    tests: list[TestEvidence] = []
    for item in tests_raw:
        if (
            type(item) is not dict
            or set(item) != {"command", "exit_code", "output_digest"}
            or type(item["command"]) is not list
        ):
            raise ValueError("local worker test payload is invalid")
        tests.append(
            TestEvidence(
                tuple(item["command"]),
                item["exit_code"],
                item["output_digest"],
            )
        )

    artifacts: list[ArtifactEvidence] = []
    for item in artifacts_raw:
        if type(item) is not dict or set(item) != {"name", "digest", "media_type"}:
            raise ValueError("local worker artifact payload is invalid")
        artifacts.append(
            ArtifactEvidence(item["name"], item["digest"], item["media_type"])
        )

    recovery_raw = payload["recovery_state"]
    recovery = None
    if recovery_raw is not None:
        if type(recovery_raw) is not dict or set(recovery_raw) != {"phase", "opaque_token"}:
            raise ValueError("local worker recovery payload is invalid")
        recovery = RecoveryState(recovery_raw["phase"], recovery_raw["opaque_token"])

    failure_raw = payload["failure"]
    failure = None
    if failure_raw is not None:
        if type(failure_raw) is not dict:
            raise ValueError("local worker failure payload is invalid")
        if set(failure_raw) != {"kind", "message", "retryable"}:
            raise ValueError("local worker failure payload has unexpected fields")
        failure = WorkerFailure(
            WorkerFailureKind(failure_raw["kind"]),
            failure_raw["message"],
            retryable=failure_raw["retryable"],
        )

    return CodingResult(
        job_id=payload["job_id"],
        changed_files=tuple(changed),
        test_evidence=tuple(tests),
        artifacts=tuple(artifacts),
        recovery_state=recovery,
        failure=failure,
    )


def _evidence_from_state(state: Mapping[str, object]) -> LocalExecutionEvidence:
    raw = state.get("evidence")
    if type(raw) is not dict or set(raw) != {
        "repository_id",
        "base_sha",
        "result_sha",
        "diff_digest",
    }:
        raise ValueError("local worker terminal evidence is invalid")
    return LocalExecutionEvidence(
        job_id=state["job_id"],
        repository_id=raw["repository_id"],
        base_sha=raw["base_sha"],
        result_sha=raw["result_sha"],
        diff_digest=raw["diff_digest"],
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("local worker state contains duplicate JSON keys")
        result[key] = value
    return result


def _finite_state_json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _bounded_state_json_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > _MAX_STATE_JSON_INTEGER_DECIMAL_CHARS:
        raise ValueError("local worker state integer exceeds the digit limit")
    value = int(raw)
    if value.bit_length() > _MAX_STATE_JSON_INTEGER_BITS:
        raise ValueError("local worker state integer exceeds the bit limit")
    return value


def _state_json_depth_is_bounded(raw: bytes) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
        elif byte == 0x22:
            quoted = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_STATE_JSON_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


@dataclass
class ContainedLocalCodingWorker(CodingWorkerPort):
    """Structured local worker over canonical private-Git/process containment.

    The worker does not invoke an LLM. Planning is injected through LocalCodingPlanPort,
    so deterministic/no-LLM, local-model and external-model planners share one mutation
    and evidence authority. Local process containment is not a hostile-code or network
    sandbox and this backend never represents it as one.
    """

    workspace_parent: pathlib.Path
    repositories: Mapping[str, pathlib.Path]
    planner: LocalCodingPlanPort
    git_executable: str = "git"
    source_environment: Mapping[str, str] | None = None
    repository_authority: LocalRepositoryExecutionAuthorityPort | None = None

    def __post_init__(self) -> None:
        parent = ensure_real_directory_root(
            pathlib.Path(self.workspace_parent),
            label="contained worker workspace parent",
        )
        self.workspace_parent = parent

        copied: dict[str, pathlib.Path] = {}
        for repository_id, root in self.repositories.items():
            identity = _safe_text(repository_id, "repository_id")
            resolved = ensure_real_directory_root(
                pathlib.Path(root),
                label=f"repository root {identity}",
            )
            if not (resolved / ".git").exists():
                raise ContainedLocalWorkerError(
                    f"repository {identity} must expose trusted Git metadata"
                )
            copied[identity] = resolved
        if not copied:
            raise ContainedLocalWorkerError("at least one local repository is required")
        self.repositories = MappingProxyType(copied)
        self.git_executable = _resolve_host_git_executable(self.git_executable)
        self.source_environment = MappingProxyType(
            dict(os.environ if self.source_environment is None else self.source_environment)
        )
        self._active: dict[str, threading.Event] = {}
        self._active_lock = threading.Lock()

    def _require_repository_authority(self, repository_id: str) -> None:
        authority = self.repository_authority
        if authority is None:
            return
        try:
            root = pathlib.Path(self.repositories[repository_id])
        except KeyError as exc:
            raise ContainedLocalWorkerError(
                "local repository identity is not configured"
            ) from exc
        authority.require_repository_root(
            repository_id=repository_id,
            root=root,
        )

    def workspace_root_for(self, job_id: str) -> pathlib.Path:
        identity = _safe_text(job_id, "job_id")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return pathlib.Path(self.workspace_parent) / f"job-{digest}"

    def ensure_workspace_root(self, job_id: str) -> pathlib.Path:
        root = self.workspace_root_for(job_id)
        try:
            root.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            pass
        return ensure_real_directory_root(root, label="contained worker job root")

    def repository_tree_digest(self, repository_id: str, base_sha: str) -> str:
        root = self._repository_root(repository_id)
        result = _git(
            (
                self.git_executable,
                "rev-parse",
                "--verify",
                f"{base_sha}^{{tree}}",
            ),
            cwd=root,
            environment=sterile_git_environment(self.source_environment),
        )
        digest = result.stdout.strip().casefold()
        if len(digest) not in {40, 64}:
            raise ContainedLocalWorkerError("Git tree identity has an unsupported length")
        if any(char not in "0123456789abcdef" for char in digest):
            raise ContainedLocalWorkerError("Git tree identity is not hexadecimal")
        return digest

    def execution_evidence(self, job_id: str) -> LocalExecutionEvidence:
        state = self._load_state(job_id)
        if state is None or state["phase"] != "terminal":
            raise ContainedLocalWorkerError("local worker has no terminal execution evidence")
        try:
            evidence = _evidence_from_state(state)
            self._require_repository_authority(evidence.repository_id)
            result = _result_from_payload(state["result"])
            self._validate_terminal_storage(evidence, result)
            return evidence
        except Exception as exc:
            raise ContainedLocalWorkerError("local worker terminal evidence is invalid") from exc

    def _validate_terminal_storage(
        self,
        evidence: LocalExecutionEvidence,
        result: CodingResult,
    ) -> None:
        if result.job_id != evidence.job_id:
            raise ContainedLocalWorkerError(
                "terminal result identity does not match execution evidence"
            )
        if evidence.result_sha == evidence.base_sha:
            if result.failure is None or result.changed_files or result.artifacts:
                raise ContainedLocalWorkerError(
                    "terminal no-candidate evidence contradicts the persisted result"
                )
            expected_diff_digest = self._terminal_diff_digest(
                repository_id=evidence.repository_id,
                base_sha=evidence.base_sha,
                result_sha=evidence.result_sha,
                tree_digest=None,
                changed_files=(),
            )
            if evidence.diff_digest.casefold() != expected_diff_digest:
                raise ContainedLocalWorkerError(
                    "terminal no-candidate diff evidence is not replay-verifiable"
                )
            return
        if not result.changed_files:
            raise ContainedLocalWorkerError(
                "terminal candidate evidence is missing changed-file proof"
            )

        repository_root = self._repository_root(evidence.repository_id)
        job_root = ensure_real_directory_root(
            self.workspace_root_for(evidence.job_id),
            label="contained worker job root",
        )
        plan = make_sterile_git_plan(
            repository_root=repository_root,
            job_root=job_root,
            branch_name=self._branch_name(evidence.job_id),
            base_sha=evidence.base_sha,
            source_environment=self.source_environment,
        )
        ensure_real_directory_root(
            plan.private_git_dir,
            label="contained worker private Git metadata",
        )
        ensure_real_directory_root(
            plan.worktree_root,
            label="contained worker candidate worktree",
        )
        prefix = (
            self.git_executable,
            *plan.config_args,
            "--git-dir",
            str(plan.private_git_dir),
            "--work-tree",
            str(plan.worktree_root),
        )
        head = _git(
            (*prefix, "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=job_root,
            environment=plan.environment,
        ).stdout.strip().casefold()
        if head != evidence.result_sha:
            raise ContainedLocalWorkerError(
                "private candidate HEAD no longer matches terminal evidence"
            )
        resolved = _git(
            (*prefix, "rev-parse", "--verify", f"{evidence.result_sha}^{{commit}}"),
            cwd=job_root,
            environment=plan.environment,
        ).stdout.strip().casefold()
        if resolved != evidence.result_sha:
            raise ContainedLocalWorkerError(
                "private candidate commit identity no longer matches terminal evidence"
            )
        lineage = _git(
            (*prefix, "rev-list", "--parents", "-n", "1", evidence.result_sha),
            cwd=job_root,
            environment=plan.environment,
        ).stdout.strip().casefold().split()
        if lineage != [evidence.result_sha, evidence.base_sha]:
            raise ContainedLocalWorkerError(
                "private candidate commit is no longer the exact child of the pinned base"
            )
        remotes = tuple(
            item.strip()
            for item in _git(
                (*prefix, "remote"),
                cwd=job_root,
                environment=plan.environment,
            ).stdout.splitlines()
            if item.strip()
        )
        if remotes:
            raise ContainedLocalWorkerError(
                "private candidate metadata unexpectedly regained a Git remote"
            )
        status = _git(
            (*prefix, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=job_root,
            environment=plan.environment,
        ).stdout
        if status:
            raise ContainedLocalWorkerError(
                "private candidate worktree changed after terminal evidence was recorded"
            )

        artifacts: dict[str, ArtifactEvidence] = {}
        for artifact in result.artifacts:
            if artifact.name in artifacts:
                raise ContainedLocalWorkerError(
                    "terminal candidate result repeats an artifact identity"
                )
            artifacts[artifact.name] = artifact
        commit_artifact = artifacts.get("candidate-git-commit")
        tree_artifact = artifacts.get("candidate-tree-sha256")
        if (
            commit_artifact is None
            or commit_artifact.digest.casefold() != evidence.result_sha
            or commit_artifact.media_type != "application/vnd.git.commit"
        ):
            raise ContainedLocalWorkerError(
                "terminal candidate commit artifact does not match execution evidence"
            )
        tree = collect_tree_evidence(plan.worktree_root)
        if (
            tree_artifact is None
            or tree_artifact.digest.casefold() != tree.digest.casefold()
            or tree_artifact.media_type != "application/vnd.nika.tree+sha256"
        ):
            raise ContainedLocalWorkerError(
                "terminal candidate tree artifact does not match current candidate bytes"
            )

        diff_paths = tuple(
            item
            for item in _git(
                (
                    *prefix,
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    evidence.base_sha,
                    evidence.result_sha,
                    "--",
                ),
                cwd=job_root,
                environment=plan.environment,
            ).stdout.split("\0")
            if item
        )
        changed_by_path: dict[str, ChangedFile] = {}
        for item in result.changed_files:
            if item.path in changed_by_path:
                raise ContainedLocalWorkerError(
                    "terminal candidate result repeats a changed-file identity"
                )
            changed_by_path[item.path] = item
        if set(diff_paths) != set(changed_by_path) or len(diff_paths) != len(changed_by_path):
            raise ContainedLocalWorkerError(
                "terminal changed-file evidence does not match the candidate commit"
            )
        files_by_path = {item.path: item for item in tree.files}
        for path, changed in changed_by_path.items():
            current = files_by_path.get(path)
            if (
                current is None
                or current.sha256.casefold() != changed.sha256.casefold()
                or current.size_bytes != changed.size_bytes
            ):
                raise ContainedLocalWorkerError(
                    "terminal changed-file evidence does not match current candidate bytes"
                )

        expected_diff_digest = self._terminal_diff_digest(
            repository_id=evidence.repository_id,
            base_sha=evidence.base_sha,
            result_sha=evidence.result_sha,
            tree_digest=tree.digest,
            changed_files=tuple(changed_by_path.values()),
        )
        if evidence.diff_digest.casefold() != expected_diff_digest:
            raise ContainedLocalWorkerError(
                "terminal candidate diff evidence is not replay-verifiable"
            )

    def candidate_worktree(self, job_id: str) -> pathlib.Path:
        evidence = self.execution_evidence(job_id)
        if evidence.result_sha == evidence.base_sha:
            raise ContainedLocalWorkerError("local worker result has no candidate worktree")
        return ensure_real_directory_root(
            self.workspace_root_for(job_id) / "worktree",
            label="contained worker candidate worktree",
        )

    def is_active(self, job_id: str) -> bool:
        identity = _safe_text(job_id, "job_id")
        with self._active_lock:
            return identity in self._active

    async def execute(self, job: CodingJob) -> CodingResult:
        try:
            exact = _snapshot_job(job)
        except (ValueError, WorkspaceSecurityError, ContainedLocalWorkerError) as exc:
            return self._failure(
                getattr(job, "job_id", "invalid-job"),
                WorkerFailureKind.INVALID_REQUEST,
                str(exc),
                retryable=False,
            )

        try:
            prior = self._load_state(exact.job_id)
        except ContainedLocalWorkerError:
            return self._manual_reconcile(exact.job_id)
        if prior is not None:
            return self._existing_result(exact, prior)

        cancellation = threading.Event()
        with self._active_lock:
            if exact.job_id in self._active:
                return self._failure(
                    exact.job_id,
                    WorkerFailureKind.INVALID_REQUEST,
                    "local coding job is already active",
                    retryable=False,
                )
            self._active[exact.job_id] = cancellation

        process_lock: _JobExecutionLock | None = None
        try:
            self._require_repository_authority(exact.repository.repository_id)
            try:
                process_lock = _JobExecutionLock(self.ensure_workspace_root(exact.job_id))
                if not process_lock.acquire():
                    return self._failure(
                        exact.job_id,
                        WorkerFailureKind.INVALID_REQUEST,
                        "local coding job is already active in another worker or process",
                        retryable=False,
                    )
            except (ValueError, WorkspaceSecurityError, ContainedLocalWorkerError):
                return self._manual_reconcile(exact.job_id)

            try:
                prior = self._load_state(exact.job_id)
            except ContainedLocalWorkerError:
                return self._manual_reconcile(exact.job_id)
            if prior is not None:
                return self._existing_result(exact, prior)

            self._require_repository_authority(exact.repository.repository_id)
            try:
                self._validate_job(exact)
            except (ValueError, WorkspaceSecurityError, ContainedLocalWorkerError) as exc:
                result = self._failure(
                    exact.job_id,
                    WorkerFailureKind.INVALID_REQUEST,
                    str(exc),
                    retryable=False,
                )
                self._save_terminal_without_candidate(exact, result)
                return result

            authority_fingerprint = _job_fingerprint(exact)
            try:
                planning_view = _snapshot_job(exact)
                proposed = await self.planner.plan(planning_view)
            except Exception:
                result = self._failure(
                    exact.job_id,
                    WorkerFailureKind.INTERNAL_ERROR,
                    "local coding planner failed without trusted evidence",
                    retryable=True,
                )
                self._save_terminal_without_candidate(exact, result)
                return result
            if _job_fingerprint(exact) != authority_fingerprint:
                return self._manual_reconcile(exact.job_id)

            self._require_repository_authority(exact.repository.repository_id)
            try:
                plan = _snapshot_plan(proposed, exact.resource_budget.max_changed_files)
                for edit in plan.edits:
                    if not exact.allowed_paths.allows(edit.path):
                        raise ContainedLocalWorkerError(
                            f"local coding plan changes path outside allowed scope: {edit.path}"
                        )
            except (ValueError, WorkspaceSecurityError, ContainedLocalWorkerError) as exc:
                result = self._failure(
                    exact.job_id,
                    WorkerFailureKind.POLICY_VIOLATION,
                    str(exc),
                    retryable=False,
                )
                self._save_terminal_without_candidate(exact, result)
                return result

            return await asyncio.to_thread(self._execute_sync, exact, plan, cancellation)
        except asyncio.CancelledError:
            cancellation.set()
            raise
        finally:
            if process_lock is not None:
                process_lock.release()
            with self._active_lock:
                self._active.pop(exact.job_id, None)

    async def cancel(self, job_id: str) -> None:
        identity = _safe_text(job_id, "job_id")
        with self._active_lock:
            event = self._active.get(identity)
        if event is not None:
            event.set()

    async def inspect(self, job_id: str) -> RecoveryState | None:
        identity = _safe_text(job_id, "job_id")
        try:
            state = self._load_state(identity)
        except ContainedLocalWorkerError:
            return RecoveryState("manual_reconcile_required", "local-state-invalid")
        if state is None:
            return None
        if state["phase"] == "terminal":
            try:
                evidence = _evidence_from_state(state)
                self._require_repository_authority(evidence.repository_id)
                result = _result_from_payload(state["result"])
                self._validate_terminal_storage(evidence, result)
            except Exception:
                return RecoveryState(
                    "manual_reconcile_required",
                    "terminal-candidate-invalid",
                )
            return RecoveryState("terminal", evidence.result_sha)
        return RecoveryState("manual_reconcile_required", "local-effect-uncertain")

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        try:
            exact = _snapshot_job(job)
            self._require_repository_authority(exact.repository.repository_id)
            self._validate_job(exact, allow_expired=True)
        except (ValueError, WorkspaceSecurityError, ContainedLocalWorkerError) as exc:
            return self._failure(
                getattr(job, "job_id", "invalid-job"),
                WorkerFailureKind.INVALID_REQUEST,
                str(exc),
                retryable=False,
            )
        if type(state) is not RecoveryState:
            return self._failure(
                exact.job_id,
                WorkerFailureKind.INVALID_REQUEST,
                "recovery state carrier is invalid",
                retryable=False,
            )
        try:
            durable = self._load_state(exact.job_id)
        except ContainedLocalWorkerError:
            durable = None
        if durable is None:
            return self._manual_reconcile(exact.job_id)
        if durable["fingerprint"] != _job_fingerprint(exact):
            return self._manual_reconcile(exact.job_id)
        if durable["phase"] != "terminal":
            return self._manual_reconcile(exact.job_id)
        self._require_repository_authority(exact.repository.repository_id)
        return self._existing_result(exact, durable)

    def _execute_sync(
        self,
        job: CodingJob,
        plan: LocalCodingPlan,
        cancellation: threading.Event,
    ) -> CodingResult:
        self._require_repository_authority(job.repository.repository_id)
        root = self.ensure_workspace_root(job.job_id)
        self._save_phase(root, job, "preparing")
        if cancellation.is_set():
            result = self._failure(
                job.job_id,
                WorkerFailureKind.CANCELLED,
                "local coding job was cancelled before workspace preparation",
                retryable=False,
            )
            self._save_terminal_without_candidate(job, result)
            return result

        repository_root = self._repository_root(job.repository.repository_id)
        git_plan = make_sterile_git_plan(
            repository_root=repository_root,
            job_root=root,
            branch_name=self._branch_name(job.job_id),
            base_sha=job.repository.base_sha,
            source_environment=self.source_environment,
        )
        try:
            prepared = prepare_private_git_workspace(
                git_plan,
                git_executable=self.git_executable,
            )
        except WorkspaceSecurityError as exc:
            return self._rollback_private_failure(
                job,
                git_plan,
                WorkerFailureKind.POLICY_VIOLATION,
                str(exc),
            )
        except Exception:
            return self._rollback_private_failure(
                job,
                git_plan,
                WorkerFailureKind.INTERNAL_ERROR,
                "contained local worker could not prove a safe candidate",
            )

        # The source copy is a repository-authority effect: a rebind, unbind or
        # filesystem-identity change while Git is cloning must not be admitted merely
        # because the copied base tree happens to retain the expected digest.
        try:
            self._require_repository_authority(job.repository.repository_id)
        except Exception:
            try:
                cleanup_private_git_workspace(git_plan)
            except Exception as cleanup_exc:
                raise ContainedLocalWorkerError(
                    "repository authority changed during private Git preparation "
                    "and cleanup could not be proven"
                ) from cleanup_exc
            raise

        try:
            if self._private_tree_digest(git_plan) != job.repository.tree_digest.casefold():
                raise WorkspaceSecurityError(
                    "private workspace tree identity does not match trusted repository snapshot"
                )

            self._save_phase(root, job, "applying")
            self._apply_plan(job, plan, prepared.plan.worktree_root)
            after = collect_tree_evidence(prepared.plan.worktree_root)
            delta = collect_tree_delta_evidence(
                prepared.tree_evidence,
                after,
                path_policy=WorkspacePathPolicy(tuple(job.allowed_paths.roots)),
                max_changed_files=job.resource_budget.max_changed_files,
            )
            self._validate_delta(delta, plan)
            result_sha = self._commit_candidate(git_plan, _job_fingerprint(job))
            changed = self._changed_files(delta)
            evidence = LocalExecutionEvidence(
                job_id=job.job_id,
                repository_id=job.repository.repository_id,
                base_sha=job.repository.base_sha.casefold(),
                result_sha=result_sha,
                diff_digest=self._terminal_diff_digest(
                    repository_id=job.repository.repository_id,
                    base_sha=job.repository.base_sha,
                    result_sha=result_sha,
                    tree_digest=after.digest,
                    changed_files=changed,
                ),
            )
            artifacts = self._candidate_artifacts(result_sha, after.digest)

            if cancellation.is_set():
                result = CodingResult(
                    job_id=job.job_id,
                    changed_files=changed,
                    artifacts=artifacts,
                    recovery_state=RecoveryState("cancelled", result_sha),
                    failure=WorkerFailure(
                        WorkerFailureKind.CANCELLED,
                        "local coding job was cancelled before acceptance",
                        retryable=False,
                    ),
                )
                self._save_terminal(job, evidence, result)
                return result

            self._save_phase(root, job, "testing", evidence=evidence)
            tests, failure = self._run_acceptance(
                job,
                prepared.plan.worktree_root,
                cancellation,
            )
            if collect_tree_evidence(prepared.plan.worktree_root).digest != after.digest:
                raise WorkspaceSecurityError(
                    "candidate worktree changed while acceptance evidence was collected"
                )
            result = CodingResult(
                job_id=job.job_id,
                changed_files=changed,
                test_evidence=tests,
                artifacts=artifacts,
                recovery_state=(
                    None
                    if failure is None
                    else RecoveryState("candidate_preserved", result_sha)
                ),
                failure=failure,
            )
            self._save_terminal(job, evidence, result)
            return result
        except WorkspaceSecurityError as exc:
            return self._rollback_private_failure(
                job,
                git_plan,
                WorkerFailureKind.POLICY_VIOLATION,
                str(exc),
            )
        except Exception:
            return self._rollback_private_failure(
                job,
                git_plan,
                WorkerFailureKind.INTERNAL_ERROR,
                "contained local worker could not prove a safe candidate",
            )

    def _validate_job(self, job: CodingJob, *, allow_expired: bool = False) -> None:
        if pathlib.Path(job.lease.workspace_root) != self.workspace_root_for(job.job_id):
            raise ContainedLocalWorkerError(
                "workspace lease root is not the deterministic local job root"
            )
        if job.lease.isolation_class is not _expected_isolation():
            raise ContainedLocalWorkerError(
                "workspace lease overclaims or mismatches local process isolation"
            )
        if not allow_expired and _parse_expiry(job.lease.expires_at) <= datetime.now(UTC):
            raise ContainedLocalWorkerError("workspace lease expired before local execution")
        if job.repository.repository_id not in self.repositories:
            raise ContainedLocalWorkerError("repository identity is not locally configured")
        expected_tree = self.repository_tree_digest(
            job.repository.repository_id,
            job.repository.base_sha,
        )
        if expected_tree != job.repository.tree_digest.casefold():
            raise ContainedLocalWorkerError(
                "trusted repository tree identity does not match the pinned base"
            )
        if job.network_policy.mode is not NetworkMode.DENY:
            raise ContainedLocalWorkerError(
                "contained local worker does not implement approved-host network enforcement"
            )
        if job.network_policy.approved_hosts:
            raise ContainedLocalWorkerError(
                "contained local worker requires an empty approved-host set"
            )
        for permission in ("read_source", "write_source"):
            if permission not in job.permission_ceiling:
                raise ContainedLocalWorkerError(
                    f"contained local worker requires {permission} permission"
                )
        if job.acceptance_commands and "run_tests" not in job.permission_ceiling:
            raise ContainedLocalWorkerError(
                "acceptance execution requires run_tests permission"
            )

    def _repository_root(self, repository_id: str) -> pathlib.Path:
        identity = _safe_text(repository_id, "repository_id")
        self._require_repository_authority(identity)
        try:
            return pathlib.Path(self.repositories[identity])
        except KeyError as exc:
            raise ContainedLocalWorkerError(
                f"repository identity is not configured: {identity}"
            ) from exc

    @staticmethod
    def _branch_name(job_id: str) -> str:
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
        return f"nika-local-{digest}"

    def _private_tree_digest(self, plan) -> str:
        prefix = (
            self.git_executable,
            *plan.config_args,
            "--git-dir",
            str(plan.private_git_dir),
        )
        result = _git(
            (*prefix, "rev-parse", "HEAD^{tree}"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        )
        return result.stdout.strip().casefold()

    @staticmethod
    def _apply_plan(job: CodingJob, plan: LocalCodingPlan, worktree: pathlib.Path) -> None:
        policy = WorkspacePathPolicy(tuple(job.allowed_paths.roots))
        for edit in plan.edits:
            target = ensure_path_policy(worktree, edit.path, policy)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = ensure_path_policy(worktree, edit.path, policy)
            if target.exists() and not target.is_file():
                raise WorkspaceSecurityError("local edit target must be a regular file")

            prior_mode = target.stat().st_mode & 0o777 if target.exists() else None
            suffix = hashlib.sha256(edit.path.encode("utf-8")).hexdigest()[:12]
            temp = target.parent / f".{target.name}.nika-{suffix}"
            if temp.exists() or temp.is_symlink():
                raise WorkspaceSecurityError("local edit temporary path already exists")
            try:
                with temp.open("xb") as handle:
                    handle.write(edit.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if prior_mode is not None:
                    os.chmod(temp, prior_mode)
                os.replace(temp, target)
            finally:
                if temp.exists():
                    temp.unlink()

    @staticmethod
    def _validate_delta(delta: TreeDeltaEvidence, plan: LocalCodingPlan) -> None:
        expected = {edit.path for edit in plan.edits}
        observed = {change.path for change in delta.changes}
        if observed != expected:
            raise WorkspaceSecurityError(
                "private workspace delta differs from the structured coding plan"
            )
        if any(change.kind == "deleted" for change in delta.changes):
            raise WorkspaceSecurityError(
                "contained local coding plans do not authorize deletion"
            )

    @staticmethod
    def _changed_files(delta: TreeDeltaEvidence) -> tuple[ChangedFile, ...]:
        changed: list[ChangedFile] = []
        for item in delta.changes:
            if item.after_sha256 is None or item.after_size_bytes is None:
                raise WorkspaceSecurityError("candidate change lost post-image evidence")
            changed.append(
                ChangedFile(item.path, item.after_sha256, item.after_size_bytes)
            )
        return tuple(changed)

    def _commit_candidate(self, plan, fingerprint: str) -> str:
        environment = dict(plan.environment)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Nika Contained Coding Worker",
                "GIT_AUTHOR_EMAIL": "nika-worker@example.invalid",
                "GIT_COMMITTER_NAME": "Nika Contained Coding Worker",
                "GIT_COMMITTER_EMAIL": "nika-worker@example.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
        )
        prefix = (
            self.git_executable,
            *plan.config_args,
            "--git-dir",
            str(plan.private_git_dir),
            "--work-tree",
            str(plan.worktree_root),
        )
        cwd = plan.private_git_dir.parent
        _git((*prefix, "add", "--all"), cwd=cwd, environment=environment)
        _git(
            (
                *prefix,
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                f"Nika contained candidate {fingerprint}",
            ),
            cwd=cwd,
            environment=environment,
        )
        sha = _git(
            (*prefix, "rev-parse", "HEAD"),
            cwd=cwd,
            environment=environment,
        ).stdout.strip().casefold()
        if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
            raise WorkspaceSecurityError("private candidate commit identity is invalid")
        return sha

    def _run_acceptance(
        self,
        job: CodingJob,
        candidate_root: pathlib.Path,
        cancellation: threading.Event,
    ) -> tuple[tuple[TestEvidence, ...], WorkerFailure | None]:
        if not job.acceptance_commands:
            return (), None
        acceptance_root = self.workspace_root_for(job.job_id) / "_acceptance"
        if acceptance_root.exists() or acceptance_root.is_symlink():
            raise WorkspaceSecurityError("acceptance workspace already exists")
        shutil.copytree(candidate_root, acceptance_root)
        ensure_real_directory_root(acceptance_root, label="acceptance workspace root")

        started = time.monotonic()
        evidence: list[TestEvidence] = []
        try:
            for command in job.acceptance_commands:
                if cancellation.is_set():
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.CANCELLED,
                        "local coding job was cancelled during acceptance",
                        retryable=False,
                    )
                remaining = job.resource_budget.timeout_seconds - (
                    time.monotonic() - started
                )
                if remaining <= 0:
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.TIMEOUT,
                        "local coding acceptance exceeded the total job deadline",
                        retryable=True,
                    )
                timeout = max(1, int(remaining))
                if command.timeout_seconds is not None:
                    timeout = min(timeout, command.timeout_seconds)
                budget = ResourceBudget(
                    timeout,
                    job.resource_budget.max_output_bytes,
                    job.resource_budget.max_changed_files,
                )
                argv = self._runtime_argv(command, job.process_policy)
                cwd = acceptance_root
                if command.cwd != ".":
                    cwd = ensure_path_policy(
                        acceptance_root,
                        command.cwd,
                        WorkspacePathPolicy((command.cwd,)),
                        must_exist=True,
                    )
                if not cwd.is_dir():
                    raise WorkspaceSecurityError(
                        "acceptance command cwd must be a directory"
                    )
                process = run_typed_process(
                    argv,
                    process_policy=job.process_policy,
                    resource_budget=budget,
                    cwd=cwd,
                    environment=self.source_environment,
                    cancellation_event=cancellation,
                    workspace_root=acceptance_root,
                )
                output_digest = hashlib.sha256(
                    (
                        process.stdout
                        + "\0"
                        + process.stderr
                        + "\0"
                        + str(process.returncode)
                    ).encode("utf-8")
                ).hexdigest()
                evidence.append(
                    TestEvidence(
                        command.argv,
                        process.returncode,
                        output_digest,
                    )
                )
                if process.cancelled:
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.CANCELLED,
                        "local coding acceptance was cancelled",
                        retryable=False,
                    )
                if process.timed_out:
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.TIMEOUT,
                        "local coding acceptance timed out",
                        retryable=True,
                    )
                if process.output_limit_exceeded:
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.PROCESS_FAILED,
                        "local coding acceptance exceeded the output limit",
                        retryable=True,
                    )
                if process.returncode != 0:
                    return tuple(evidence), WorkerFailure(
                        WorkerFailureKind.PROCESS_FAILED,
                        "local coding acceptance command failed",
                        retryable=True,
                    )
            return tuple(evidence), None
        finally:
            if acceptance_root.exists() or acceptance_root.is_symlink():
                assert_cleanup_tree_safe(acceptance_root)
                shutil.rmtree(acceptance_root)

    @staticmethod
    def _runtime_argv(
        command: AcceptanceCommand,
        policy: ProcessPolicy,
    ) -> tuple[str, ...]:
        declared = command.argv[0]
        if pathlib.Path(declared).is_absolute():
            return command.argv
        declared_name = pathlib.PureWindowsPath(declared).name.casefold()
        matches = [
            item
            for item in policy.allowed_executables
            if pathlib.Path(item).is_absolute()
            and pathlib.PureWindowsPath(item).name.casefold() == declared_name
        ]
        if len(matches) != 1:
            raise WorkspaceSecurityError(
                "acceptance executable must map to exactly one pinned absolute executable"
            )
        return (matches[0], *command.argv[1:])

    @staticmethod
    def _terminal_diff_digest(
        *,
        repository_id: str,
        base_sha: str,
        result_sha: str,
        tree_digest: str | None,
        changed_files: tuple[ChangedFile, ...],
    ) -> str:
        """Build replay-verifiable source evidence from terminal authorities."""

        identity = _safe_text(repository_id, "repository_id")
        normalized_base = base_sha.casefold()
        normalized_result = result_sha.casefold()
        for value, label in (
            (normalized_base, "base_sha"),
            (normalized_result, "result_sha"),
        ):
            if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
                raise ContainedLocalWorkerError(
                    f"terminal {label} is not a canonical Git commit identity"
                )

        if normalized_result == normalized_base:
            if tree_digest is not None or changed_files:
                raise ContainedLocalWorkerError(
                    "no-change terminal evidence cannot carry candidate source state"
                )
            normalized_tree = "-"
        else:
            if (
                type(tree_digest) is not str
                or len(tree_digest) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in tree_digest.casefold()
                )
            ):
                raise ContainedLocalWorkerError(
                    "terminal candidate tree digest is invalid"
                )
            if not changed_files:
                raise ContainedLocalWorkerError(
                    "terminal candidate diff evidence requires changed files"
                )
            normalized_tree = tree_digest.casefold()

        hasher = hashlib.sha256()
        hasher.update(b"nika-contained-terminal-diff-v1\0")
        hasher.update(identity.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(normalized_base.encode("ascii"))
        hasher.update(b"\0")
        hasher.update(normalized_result.encode("ascii"))
        hasher.update(b"\0")
        hasher.update(normalized_tree.encode("ascii"))
        hasher.update(b"\n")

        seen: set[str] = set()
        for item in sorted(changed_files, key=lambda value: value.path.casefold()):
            if type(item) is not ChangedFile:
                raise ContainedLocalWorkerError(
                    "terminal diff evidence contains an invalid changed-file carrier"
                )
            folded = item.path.casefold()
            if folded in seen:
                raise ContainedLocalWorkerError(
                    "terminal diff evidence repeats a path identity"
                )
            seen.add(folded)
            hasher.update(item.path.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(item.sha256.casefold().encode("ascii"))
            hasher.update(b"\0")
            hasher.update(str(item.size_bytes).encode("ascii"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    @staticmethod
    def _candidate_artifacts(
        result_sha: str,
        tree_digest: str,
    ) -> tuple[ArtifactEvidence, ...]:
        return (
            ArtifactEvidence(
                "candidate-git-commit",
                result_sha,
                "application/vnd.git.commit",
            ),
            ArtifactEvidence(
                "candidate-tree-sha256",
                tree_digest,
                "application/vnd.nika.tree+sha256",
            ),
        )

    def _rollback_private_failure(
        self,
        job: CodingJob,
        git_plan,
        kind: WorkerFailureKind,
        message: str,
    ) -> CodingResult:
        try:
            cleanup_private_git_workspace(git_plan)
        except Exception:
            result = self._manual_reconcile(job.job_id)
            self._save_terminal_without_candidate(job, result)
            return result
        result = self._failure(job.job_id, kind, message, retryable=False)
        self._save_terminal_without_candidate(job, result)
        return result

    def _save_phase(
        self,
        root: pathlib.Path,
        job: CodingJob,
        phase: str,
        *,
        evidence: LocalExecutionEvidence | None = None,
    ) -> None:
        self._save_state(
            root,
            {
                "schema": _STATE_SCHEMA,
                "phase": phase,
                "job_id": job.job_id,
                "fingerprint": _job_fingerprint(job),
                "evidence": self._evidence_payload(evidence),
                "result": None,
            },
        )

    def _save_terminal_without_candidate(
        self,
        job: CodingJob,
        result: CodingResult,
    ) -> None:
        evidence = LocalExecutionEvidence(
            job_id=job.job_id,
            repository_id=job.repository.repository_id,
            base_sha=job.repository.base_sha.casefold(),
            result_sha=job.repository.base_sha.casefold(),
            diff_digest=self._terminal_diff_digest(
                repository_id=job.repository.repository_id,
                base_sha=job.repository.base_sha,
                result_sha=job.repository.base_sha,
                tree_digest=None,
                changed_files=(),
            ),
        )
        self._save_terminal(job, evidence, result)

    def _save_terminal(
        self,
        job: CodingJob,
        evidence: LocalExecutionEvidence,
        result: CodingResult,
    ) -> None:
        root = self.ensure_workspace_root(job.job_id)
        self._save_state(
            root,
            {
                "schema": _STATE_SCHEMA,
                "phase": "terminal",
                "job_id": job.job_id,
                "fingerprint": _job_fingerprint(job),
                "evidence": self._evidence_payload(evidence),
                "result": _result_payload(result),
            },
        )

    @staticmethod
    def _evidence_payload(
        evidence: LocalExecutionEvidence | None,
    ) -> dict[str, str] | None:
        if evidence is None:
            return None
        return {
            "repository_id": evidence.repository_id,
            "base_sha": evidence.base_sha,
            "result_sha": evidence.result_sha,
            "diff_digest": evidence.diff_digest,
        }

    @staticmethod
    def _save_state(root: pathlib.Path, payload: dict[str, object]) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > _MAX_STATE_BYTES:
            raise ContainedLocalWorkerError("local worker state exceeds the byte limit")
        state_path = root / "_nika_local_worker_state.json"
        fd, temp_name = tempfile.mkstemp(prefix=".nika-state-", dir=root)
        temp = pathlib.Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, state_path)
        finally:
            if temp.exists():
                temp.unlink()

    def _load_state(self, job_id: str) -> dict[str, object] | None:
        root = self.workspace_root_for(job_id)
        if not root.exists():
            return None
        root = ensure_real_directory_root(root, label="contained worker job root")
        state_path = root / "_nika_local_worker_state.json"
        if not state_path.exists():
            return None
        state_path = ensure_path_policy(
            root,
            state_path.name,
            WorkspacePathPolicy((state_path.name,)),
            must_exist=True,
        )
        if not state_path.is_file():
            raise ContainedLocalWorkerError("local worker state path is unsafe")
        with state_path.open("rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            raise ContainedLocalWorkerError("local worker state exceeds the byte limit")
        if not _state_json_depth_is_bounded(raw):
            raise ContainedLocalWorkerError("local worker state is invalid")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_float=_finite_state_json_float,
                parse_int=_bounded_state_json_int,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ContainedLocalWorkerError("local worker state is invalid") from exc
        if type(payload) is not dict:
            raise ContainedLocalWorkerError("local worker state root is invalid")
        if set(payload) != {
            "schema",
            "phase",
            "job_id",
            "fingerprint",
            "evidence",
            "result",
        }:
            raise ContainedLocalWorkerError("local worker state fields are invalid")
        if payload["schema"] != _STATE_SCHEMA or payload["job_id"] != job_id:
            raise ContainedLocalWorkerError("local worker state identity is invalid")
        if payload["phase"] not in {"preparing", "applying", "testing", "terminal"}:
            raise ContainedLocalWorkerError("local worker state phase is invalid")
        fingerprint = payload["fingerprint"]
        if type(fingerprint) is not str or len(fingerprint) != 64:
            raise ContainedLocalWorkerError("local worker state fingerprint is invalid")
        if any(char not in "0123456789abcdef" for char in fingerprint):
            raise ContainedLocalWorkerError("local worker state fingerprint is invalid")
        return payload

    def _existing_result(
        self,
        job: CodingJob,
        state: dict[str, object],
    ) -> CodingResult:
        if state["fingerprint"] != _job_fingerprint(job):
            return self._manual_reconcile(job.job_id)
        if state["phase"] != "terminal":
            return self._manual_reconcile(job.job_id)
        try:
            evidence = _evidence_from_state(state)
            result = _result_from_payload(state["result"])
            if (
                evidence.job_id != job.job_id
                or evidence.repository_id != job.repository.repository_id
                or evidence.base_sha != job.repository.base_sha.casefold()
            ):
                raise ContainedLocalWorkerError(
                    "terminal evidence identity does not match the requested job"
                )
            self._require_repository_authority(job.repository.repository_id)
            self._validate_terminal_storage(evidence, result)
            return result
        except Exception:
            return self._manual_reconcile(job.job_id)

    @staticmethod
    def _failure(
        job_id: str,
        kind: WorkerFailureKind,
        message: str,
        *,
        retryable: bool,
    ) -> CodingResult:
        identity = job_id if type(job_id) is str and job_id else "invalid-job"
        return CodingResult(
            job_id=identity,
            failure=WorkerFailure(kind, message, retryable=retryable),
        )

    @staticmethod
    def _manual_reconcile(job_id: str) -> CodingResult:
        return CodingResult(
            job_id=job_id,
            recovery_state=RecoveryState(
                "manual_reconcile_required",
                "contained-local-effect-uncertain",
            ),
            failure=WorkerFailure(
                WorkerFailureKind.INTERNAL_ERROR,
                "contained local worker requires explicit reconciliation",
                retryable=False,
            ),
        )

    @staticmethod
    def _no_change_digest(job: CodingJob) -> str:
        raw = (
            "nika-contained-no-change-v1\0"
            + job.repository.repository_id
            + "\0"
            + job.repository.base_sha.casefold()
            + "\0"
            + job.repository.tree_digest
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
