from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import pathlib
import tempfile
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    IsolationClass,
    NetworkMode,
    RecoveryState,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)
from nika_core.toolsmith.execution import run_typed_process
from nika_core.toolsmith.workspace_security import (
    TreeEvidence,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    collect_tree_evidence,
    ensure_path_policy,
    sterile_git_environment,
)


class OpenHandsWorkerError(RuntimeError):
    """Raised when a remote coding run cannot satisfy Nika's worker contract."""


class OpenHandsWorkspaceMutationError(RuntimeError):
    """Raised when a local staging mutation cannot be proven rolled back."""


@dataclasses.dataclass(frozen=True, slots=True)
class OpenHandsSandboxEndpoint:
    """Attested control-plane endpoint for one fresh remote sandbox.

    The endpoint deliberately carries no raw secret. Authentication material stays
    behind the injected workspace factory/credential authority.
    """

    endpoint_id: str
    host: str
    working_dir: str
    isolation_class: IsolationClass
    sandbox_egress_hosts: tuple[str, ...] = ()
    fresh_workspace: bool = True

    def __post_init__(self) -> None:
        if not self.endpoint_id.strip() or not self.host.strip() or not self.working_dir.strip():
            raise ValueError("OpenHands endpoint identity, host and working_dir are required")
        parsed = urlparse(self.host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OpenHands endpoint host must be an absolute http(s) URL")
        if not self.working_dir.startswith("/"):
            raise ValueError("OpenHands remote working_dir must be an absolute POSIX path")
        if self.isolation_class is not IsolationClass.REMOTE_SANDBOXED:
            raise ValueError("OpenHands endpoint must attest REMOTE_SANDBOXED isolation")
        if not self.fresh_workspace:
            raise ValueError("OpenHands endpoint must provide a fresh per-job workspace")
        if any(not host.strip() for host in self.sandbox_egress_hosts):
            raise ValueError("OpenHands sandbox egress hosts must not be empty")

    @property
    def control_plane_host(self) -> str:
        parsed = urlparse(self.host)
        assert parsed.hostname is not None
        return parsed.hostname.casefold()


@dataclasses.dataclass(frozen=True, slots=True)
class RemoteFile:
    path: str
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class OpenHandsRunEvidence:
    conversation_id: str
    files: tuple[RemoteFile, ...]

    def __post_init__(self) -> None:
        if not self.conversation_id.strip():
            raise ValueError("OpenHands run evidence requires a conversation id")


@runtime_checkable
class OpenHandsSandboxProviderPort(Protocol):
    async def acquire(self, job: CodingJob) -> OpenHandsSandboxEndpoint: ...

    async def release(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        *,
        succeeded: bool,
    ) -> None: ...


@runtime_checkable
class OpenHandsRemoteRuntimePort(Protocol):
    async def execute(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        prompt: str,
        source_root: pathlib.Path,
        source_evidence: TreeEvidence,
    ) -> OpenHandsRunEvidence: ...

    async def cancel(self, job_id: str) -> None: ...


class OpenHandsRemoteCodingWorker(CodingWorkerPort):
    """Nika CodingWorker backed by a fresh, remotely sandboxed OpenHands runtime.

    Nika remains authoritative for policy, local workspace identity, changed-path
    acceptance, process execution and test evidence. The external coding engine never
    receives production Git metadata or GitHub credentials and cannot publish itself.
    """

    def __init__(
        self,
        sandbox_provider: OpenHandsSandboxProviderPort,
        runtime: OpenHandsRemoteRuntimePort,
    ) -> None:
        self._sandbox_provider = sandbox_provider
        self._runtime = runtime
        self._states: dict[str, RecoveryState] = {}
        self._lock = asyncio.Lock()

    async def execute(self, job: CodingJob) -> CodingResult:
        async with self._lock:
            if job.job_id in self._states and self._states[job.job_id].phase == "running":
                return _failure_result(
                    job,
                    WorkerFailureKind.INVALID_REQUEST,
                    "coding job is already running",
                    retryable=False,
                )
            self._states[job.job_id] = RecoveryState("running")

        endpoint: OpenHandsSandboxEndpoint | None = None
        local_root: pathlib.Path | None = None
        changed: tuple[ChangedFile, ...] = ()
        tests: tuple[TestEvidence, ...] = ()
        applied = False
        succeeded = False
        try:
            local_root = _validate_local_workspace(job)
            source_evidence = collect_tree_evidence(local_root)
            _validate_source_identity(job, source_evidence)
            endpoint = await self._sandbox_provider.acquire(job)
            _validate_endpoint(job, endpoint)
            prompt = _build_prompt(job)
            run = await asyncio.wait_for(
                self._runtime.execute(job, endpoint, prompt, local_root, source_evidence),
                timeout=job.resource_budget.timeout_seconds,
            )
            changed = _validate_and_apply_snapshot(job, local_root, source_evidence, run.files)
            applied = True
            tests = _run_acceptance(job, local_root)
            failed_test = next((evidence for evidence in tests if evidence.exit_code != 0), None)
            if failed_test is not None:
                state = RecoveryState("repair_required", run.conversation_id)
                await self._set_state(job.job_id, state)
                return CodingResult(
                    job_id=job.job_id,
                    changed_files=changed,
                    test_evidence=tests,
                    recovery_state=state,
                    failure=WorkerFailure(
                        WorkerFailureKind.PROCESS_FAILED,
                        "one or more Nika acceptance commands failed",
                        retryable=True,
                    ),
                )
            succeeded = True
            await self._set_state(job.job_id, RecoveryState("completed", run.conversation_id))
            return CodingResult(
                job_id=job.job_id,
                changed_files=changed,
                test_evidence=tests,
                recovery_state=RecoveryState("completed", run.conversation_id),
            )
        except TimeoutError:
            await self._runtime.cancel(job.job_id)
            state = RecoveryState("interrupted")
            await self._set_state(job.job_id, state)
            return _failure_result(
                job,
                WorkerFailureKind.TIMEOUT,
                "remote coding worker exceeded its Nika resource deadline",
                retryable=not applied,
                state=state,
                changed_files=changed,
                test_evidence=tests,
            )
        except asyncio.CancelledError:
            await self._runtime.cancel(job.job_id)
            state = RecoveryState("cancelled")
            await self._set_state(job.job_id, state)
            raise
        except OpenHandsWorkspaceMutationError:
            state = RecoveryState("manual_reconcile_required")
            await self._set_state(job.job_id, state)
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "local staging mutation could not be proven rolled back",
                retryable=False,
                state=state,
                changed_files=changed,
                test_evidence=tests,
            )
        except (WorkspaceSecurityError, ValueError, OpenHandsWorkerError) as exc:
            state = RecoveryState("blocked")
            await self._set_state(job.job_id, state)
            return _failure_result(
                job,
                WorkerFailureKind.POLICY_VIOLATION,
                _safe_message(exc, "remote coding result violated Nika policy"),
                retryable=False,
                state=state,
                changed_files=changed,
                test_evidence=tests,
            )
        except Exception:
            state = RecoveryState("interrupted")
            await self._set_state(job.job_id, state)
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "remote coding engine failed without trusted diagnostics",
                retryable=not applied,
                state=state,
                changed_files=changed,
                test_evidence=tests,
            )
        finally:
            if endpoint is not None:
                try:
                    await self._sandbox_provider.release(job, endpoint, succeeded=succeeded)
                except Exception:
                    # Release failures must not replace the typed worker result. The
                    # provider is responsible for durable cleanup/reconciliation.
                    pass

    async def cancel(self, job_id: str) -> None:
        await self._runtime.cancel(job_id)
        await self._set_state(job_id, RecoveryState("cancelled"))

    async def inspect(self, job_id: str) -> RecoveryState | None:
        async with self._lock:
            return self._states.get(job_id)

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        if state.phase not in {"interrupted", "cancelled"}:
            return _failure_result(
                job,
                WorkerFailureKind.INVALID_REQUEST,
                "recovery state is not restartable",
                retryable=False,
                state=state,
            )
        # Only pre-apply interruption/cancellation is restartable with the same snapshot.
        # Failed acceptance already changed the private staging tree and requires a new
        # Nika-owned repository snapshot/job before another coding attempt.
        return await self.execute(job)

    async def _set_state(self, job_id: str, state: RecoveryState) -> None:
        async with self._lock:
            self._states[job_id] = state


def _validate_source_identity(job: CodingJob, evidence: TreeEvidence) -> None:
    expected = job.repository.tree_digest.casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise OpenHandsWorkerError(
            "remote coding requires a canonical sha256 repository tree digest"
        )
    if evidence.digest.casefold() != expected:
        raise OpenHandsWorkerError(
            "local staging workspace no longer matches repository tree evidence"
        )


def _validate_local_workspace(job: CodingJob) -> pathlib.Path:
    if job.lease.isolation_class is IsolationClass.POLICY_ONLY:
        raise OpenHandsWorkerError("local staging workspace lacks enforced process isolation")
    try:
        expires = datetime.fromisoformat(job.lease.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpenHandsWorkerError("workspace lease has an invalid expiry") from exc
    if expires.tzinfo is None or expires.utcoffset() is None:
        raise OpenHandsWorkerError("workspace lease expiry must be timezone-aware")
    if expires.astimezone(UTC) <= datetime.now(UTC):
        raise OpenHandsWorkerError("workspace lease has expired")
    root = job.lease.workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise OpenHandsWorkerError("local staging workspace is not a directory")
    if (root / ".git").exists():
        raise OpenHandsWorkerError("worker-visible staging workspace must not expose .git metadata")
    return root


def _normalize_host(value: str) -> str:
    candidate = value.strip().casefold().rstrip(".")
    if not candidate or "/" in candidate or ":" in candidate:
        raise OpenHandsWorkerError("network policy host entries must be bare host names")
    return candidate


def _validate_endpoint(job: CodingJob, endpoint: OpenHandsSandboxEndpoint) -> None:
    if job.network_policy.mode is not NetworkMode.APPROVED_HOSTS:
        raise OpenHandsWorkerError("remote coding requires explicit approved-host network policy")
    approved = {_normalize_host(host) for host in job.network_policy.approved_hosts}
    if endpoint.control_plane_host not in {"127.0.0.1", "localhost"}:
        raise OpenHandsWorkerError("current OpenHands control plane must be loopback-local")
    required = {endpoint.control_plane_host}
    required.update(_normalize_host(host) for host in endpoint.sandbox_egress_hosts)
    if not required <= approved:
        raise OpenHandsWorkerError("remote coding endpoint or sandbox egress is not approved")


def _build_prompt(job: CodingJob) -> str:
    allowed = "\n".join(f"- {root}" for root in job.allowed_paths.roots)
    return (
        "You are a coding engine operating inside a disposable Nika sandbox.\n"
        "Do not commit, push, authenticate to GitHub, inspect host secrets, or change "
        "files outside the allowed paths. Nika will independently validate and test "
        "every returned byte. Acceptance command arguments are intentionally withheld "
        "from this external engine.\n\n"
        f"Goal:\n{job.goal}\n\nAllowed paths:\n{allowed}\n"
    )


def _validate_and_apply_snapshot(
    job: CodingJob,
    local_root: pathlib.Path,
    before: TreeEvidence,
    remote_files: tuple[RemoteFile, ...],
) -> tuple[ChangedFile, ...]:
    before_map = {item.path: item for item in before.files}
    staged: dict[str, RemoteFile] = {}
    total_bytes = 0
    for item in remote_files:
        path = item.path.replace("\\", "/")
        if path in staged:
            raise OpenHandsWorkerError("remote snapshot contains duplicate file paths")
        # Canonical Nika path validation is performed by ensure_path_policy below.
        staged[path] = RemoteFile(path, item.data)
        total_bytes += len(item.data)
        if total_bytes > 256 * 1024 * 1024:
            raise OpenHandsWorkerError("remote snapshot exceeds Nika evidence byte limit")
        if len(staged) > 2000:
            raise OpenHandsWorkerError("remote snapshot exceeds Nika evidence file limit")

    deleted = sorted(set(before_map) - set(staged))
    if deleted:
        raise OpenHandsWorkerError("remote coding backend does not yet accept file deletion")

    changed_paths: list[str] = []
    for path, item in staged.items():
        digest = hashlib.sha256(item.data).hexdigest()
        prior = before_map.get(path)
        if prior is None or prior.sha256 != digest or prior.size_bytes != len(item.data):
            changed_paths.append(path)

    changed_paths.sort(key=str.casefold)
    if len(changed_paths) > job.resource_budget.max_changed_files:
        raise OpenHandsWorkerError("remote coding result exceeds max_changed_files")

    policy = WorkspacePathPolicy(job.allowed_paths.roots)
    destinations: list[tuple[pathlib.Path, RemoteFile]] = []
    for path in changed_paths:
        destination = ensure_path_policy(local_root, path, policy)
        if destination.exists() and not destination.is_file():
            raise OpenHandsWorkerError("remote snapshot would replace a non-file path")
        destinations.append((destination, staged[path]))

    backups: list[tuple[pathlib.Path, bytes | None, int | None]] = []
    try:
        for destination, item in destinations:
            exists = destination.is_file()
            previous = destination.read_bytes() if exists else None
            previous_mode = destination.stat().st_mode & 0o777 if exists else None
            backups.append((destination, previous, previous_mode))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.nika-",
                delete=False,
            ) as handle:
                handle.write(item.data)
                temporary = pathlib.Path(handle.name)
            if previous_mode is not None:
                temporary.chmod(previous_mode)
            os.replace(temporary, destination)
        after = collect_tree_evidence(local_root)
    except Exception:
        rollback_failed = False
        for destination, previous, previous_mode in reversed(backups):
            try:
                if previous is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.write_bytes(previous)
                    if previous_mode is not None:
                        destination.chmod(previous_mode)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise OpenHandsWorkspaceMutationError(
                "local staging mutation could not be proven rolled back"
            ) from None
        raise

    after_map = {item.path: item for item in after.files}
    effective = sorted(
        (
            path
            for path in set(before_map) | set(after_map)
            if before_map.get(path) != after_map.get(path)
        ),
        key=str.casefold,
    )
    if effective != changed_paths:
        raise OpenHandsWorkerError("local post-apply evidence differs from validated remote delta")

    return tuple(
        ChangedFile(path, after_map[path].sha256, after_map[path].size_bytes)
        for path in changed_paths
    )


def _run_acceptance(job: CodingJob, root: pathlib.Path) -> tuple[TestEvidence, ...]:
    evidence: list[TestEvidence] = []
    environment = sterile_git_environment(os.environ)
    for command in job.acceptance_commands:
        cwd = root
        if command.cwd != ".":
            policy = WorkspacePathPolicy((command.cwd,))
            cwd = ensure_path_policy(root, command.cwd, policy, must_exist=True)
            if not cwd.is_dir():
                raise OpenHandsWorkerError("acceptance command cwd is not a directory")
        timeout_seconds = command.timeout_seconds or job.resource_budget.timeout_seconds
        budget = ResourceBudget(
            timeout_seconds=timeout_seconds,
            max_output_bytes=job.resource_budget.max_output_bytes,
            max_changed_files=job.resource_budget.max_changed_files,
        )
        result = run_typed_process(
            command.argv,
            process_policy=job.process_policy,
            resource_budget=budget,
            cwd=cwd,
            environment=environment,
        )
        output_digest = hashlib.sha256(
            result.stdout.encode("utf-8") + b"\x00" + result.stderr.encode("utf-8")
        ).hexdigest()
        evidence.append(TestEvidence(result.argv, result.returncode, output_digest))
        if result.timed_out or result.cancelled or result.output_limit_exceeded:
            break
    return tuple(evidence)


def _failure_result(
    job: CodingJob,
    kind: WorkerFailureKind,
    message: str,
    *,
    retryable: bool,
    state: RecoveryState | None = None,
    changed_files: tuple[ChangedFile, ...] = (),
    test_evidence: tuple[TestEvidence, ...] = (),
) -> CodingResult:
    return CodingResult(
        job_id=job.job_id,
        changed_files=changed_files,
        test_evidence=test_evidence,
        recovery_state=state,
        failure=WorkerFailure(kind, message, retryable=retryable),
    )


def _safe_message(error: BaseException, fallback: str) -> str:
    message = str(error).strip()
    if (
        not message
        or len(message) > 300
        or "token" in message.casefold()
        or "secret" in message.casefold()
    ):
        return fallback
    return message
