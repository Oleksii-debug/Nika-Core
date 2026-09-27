from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .contracts import (
    ChangedFile,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    normalize_relative_path,
)
from .workspace_security import validate_typed_argv

_CONVERSATION_NAMESPACE = uuid.UUID("bca670e4-daf4-4a84-b06e-85e9ae48408f")
_TERMINAL_STATUSES = frozenset({"finished", "error", "stuck"})
_PAUSED_STATUSES = frozenset({"paused", "waiting_for_confirmation"})
_MAX_GOAL_CHARS = 32768


class OpenHandsRemoteWorkerError(ValueError):
    """Configuration or trusted-binding error for the OpenHands backend."""


@dataclass(frozen=True, slots=True)
class OpenHandsRemoteWorkerConfig:
    """Connection-only configuration for a pre-provisioned OpenHands Agent Server.

    LLM/provider credentials deliberately do not cross this boundary. The remote
    deployment owns a pre-created agent_profile_id while Nika owns the
    CodingJob policy, exact repository identity, recovery state and evidence.
    """

    base_url: str
    agent_profile_id: str
    session_api_key: str | None = field(default=None, repr=False)
    request_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    max_iterations: int = 200
    allow_insecure_private_http: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise OpenHandsRemoteWorkerError("OpenHands base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise OpenHandsRemoteWorkerError(
                "OpenHands base_url must not contain credentials, query data or fragments"
            )
        try:
            uuid.UUID(self.agent_profile_id)
        except (ValueError, AttributeError) as exc:
            raise OpenHandsRemoteWorkerError("agent_profile_id must be a UUID") from exc
        if self.request_timeout_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise OpenHandsRemoteWorkerError("OpenHands timeouts must be positive")
        if self.max_iterations <= 0:
            raise OpenHandsRemoteWorkerError("max_iterations must be positive")

        loopback = _is_loopback_host(parsed.hostname)
        if not loopback and not self.session_api_key:
            raise OpenHandsRemoteWorkerError(
                "remote OpenHands endpoints require X-Session-API-Key authentication"
            )
        if parsed.scheme != "https" and not loopback and not self.allow_insecure_private_http:
            raise OpenHandsRemoteWorkerError(
                "remote OpenHands HTTP requires explicit private-network opt-in; prefer HTTPS"
            )


@dataclass(frozen=True, slots=True)
class OpenHandsSandboxAttestation:
    """Trusted provisioner statement binding one CodingJob to a remote sandbox.

    This is not inferred from OpenHands self-report. A trusted deployment/provisioner
    must bind these values before Nika grants the backend execution authority.
    """

    lease_id: str
    repository_id: str
    base_sha: str
    tree_digest: str
    workspace_root: str
    isolation_class: IsolationClass
    expires_at: str
    allowed_paths: tuple[str, ...]
    process_policy: ProcessPolicy
    network_policy: NetworkPolicy
    resource_budget: ResourceBudget
    permission_ceiling: frozenset[str]
    server_build_sha: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.lease_id,
                self.repository_id,
                self.base_sha,
                self.tree_digest,
                self.workspace_root,
                self.expires_at,
            )
        ):
            raise OpenHandsRemoteWorkerError("sandbox attestation identity must not be empty")
        if self.isolation_class is not IsolationClass.REMOTE_SANDBOXED:
            raise OpenHandsRemoteWorkerError(
                "OpenHands backend requires REMOTE_SANDBOXED authority"
            )
        if not self.workspace_root.startswith("/"):
            raise OpenHandsRemoteWorkerError(
                "OpenHands remote sandbox workspace must use an absolute POSIX path"
            )
        for root in self.allowed_paths:
            normalize_relative_path(root)
        if self.server_build_sha is not None:
            _validate_git_sha(self.server_build_sha, "server_build_sha")


class OpenHandsSandboxAttestor(Protocol):
    async def attest(
        self,
        job: CodingJob,
        server_info: Mapping[str, object],
    ) -> OpenHandsSandboxAttestation: ...


@dataclass(frozen=True, slots=True)
class PinnedOpenHandsSandboxAttestor:
    """Small adapter for provisioner-issued immutable attestation evidence."""

    attestation: OpenHandsSandboxAttestation

    async def attest(
        self,
        job: CodingJob,
        server_info: Mapping[str, object],
    ) -> OpenHandsSandboxAttestation:
        expected = self.attestation.server_build_sha
        if expected is not None:
            observed = _server_build_sha(server_info)
            if observed != expected:
                raise OpenHandsRemoteWorkerError("OpenHands server build identity mismatch")
        return self.attestation


@dataclass(frozen=True, slots=True)
class _RemoteProblem(Exception):
    kind: WorkerFailureKind
    message: str
    retryable: bool = False


class OpenHandsRemoteCodingWorker(CodingWorkerPort):
    """Production CodingWorkerPort adapter for a pre-provisioned remote sandbox.

    OpenHands supplies the maintained coding engine. Nika remains authoritative for
    job scope, permission ceiling, repository/base identity, resource bounds,
    recovery and the evidence returned to Product Factory. The adapter never passes
    Nika secrets or raw model credentials to a conversation payload.
    """

    def __init__(
        self,
        config: OpenHandsRemoteWorkerConfig,
        attestor: OpenHandsSandboxAttestor,
        *,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._attestor = attestor
        self._client_factory = client_factory
        self._sleep = sleep
        self._clock = clock

    async def execute(self, job: CodingJob) -> CodingResult:
        conversation_id = _conversation_id(job.job_id)
        creation_attempted = False
        deadline = self._clock() + job.resource_budget.timeout_seconds
        try:
            self._validate_job(job)
            async with self._client() as client:
                await self._preflight(client, job)
                creation_attempted = True
                await self._create_or_resume_conversation(client, job, conversation_id)
                status = await self._poll_until_stopped(client, job, conversation_id, deadline)
                return await self._result_for_status(
                    client, job, conversation_id, status, deadline
                )
        except _RemoteProblem as exc:
            return _failure_result(job, exc, recovery_possible=creation_attempted)
        except (OpenHandsRemoteWorkerError, ValueError, TypeError) as exc:
            return _failure_result(
                job,
                _RemoteProblem(WorkerFailureKind.INVALID_REQUEST, str(exc), False),
            )

    async def cancel(self, job_id: str) -> None:
        conversation_id = _conversation_id(job_id)
        async with self._client() as client:
            response = await self._safe_request(
                client,
                "POST",
                f"/api/conversations/{conversation_id}/interrupt",
                accepted={200, 404},
            )
            if response.status_code == 404:
                return

    async def inspect(self, job_id: str) -> RecoveryState | None:
        conversation_id = _conversation_id(job_id)
        async with self._client() as client:
            response = await self._safe_request(
                client,
                "GET",
                f"/api/conversations/{conversation_id}",
                accepted={200, 404},
            )
            if response.status_code == 404:
                return None
            body = _json_object(response, "conversation inspection")
            status = _conversation_status(body)
            return RecoveryState(status, str(conversation_id))

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        conversation_id = _conversation_id(job.job_id)
        deadline = self._clock() + job.resource_budget.timeout_seconds
        if state.opaque_token != str(conversation_id):
            return _failure_result(
                job,
                _RemoteProblem(
                    WorkerFailureKind.INVALID_REQUEST,
                    "recovery token does not match deterministic OpenHands conversation identity",
                    False,
                ),
            )
        try:
            self._validate_job(job)
            async with self._client() as client:
                await self._ready_and_attest(client, job)
                body = await self._get_conversation(client, conversation_id)
                self._validate_conversation_identity(body, job, conversation_id)
                status = _conversation_status(body)
                if status == "finished":
                    return await self._collect_success(client, job, deadline)
                if status in _TERMINAL_STATUSES:
                    return await self._result_for_status(
                        client, job, conversation_id, status, deadline
                    )
                if status in _PAUSED_STATUSES or status in {"idle", "stopped"}:
                    await self._safe_request(
                        client,
                        "POST",
                        f"/api/conversations/{conversation_id}/run",
                        accepted={200, 202, 409},
                    )
                status = await self._poll_until_stopped(
                    client, job, conversation_id, deadline
                )
                return await self._result_for_status(
                    client, job, conversation_id, status, deadline
                )
        except _RemoteProblem as exc:
            return _failure_result(job, exc, recovery_possible=True)
        except (OpenHandsRemoteWorkerError, ValueError, TypeError) as exc:
            return _failure_result(
                job,
                _RemoteProblem(WorkerFailureKind.INVALID_REQUEST, str(exc), False),
            )

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self._config.session_api_key:
            headers["X-Session-API-Key"] = self._config.session_api_key
        return self._client_factory(
            base_url=self._config.base_url.rstrip("/"),
            headers=headers,
            timeout=self._config.request_timeout_seconds,
            follow_redirects=False,
        )

    def _validate_job(self, job: CodingJob) -> None:
        if job.lease.isolation_class is not IsolationClass.REMOTE_SANDBOXED:
            raise OpenHandsRemoteWorkerError(
                "OpenHands worker accepts only REMOTE_SANDBOXED workspace leases"
            )
        if len(job.goal) > _MAX_GOAL_CHARS:
            raise OpenHandsRemoteWorkerError("coding goal exceeds OpenHands request bound")
        if not job.lease.workspace_root.as_posix().startswith("/"):
            raise OpenHandsRemoteWorkerError(
                "OpenHands remote workspace must be represented by an absolute POSIX path"
            )
        for command in job.acceptance_commands:
            validate_typed_argv(command.argv, job.process_policy.allowed_executables)

    async def _ready_and_attest(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
    ) -> OpenHandsSandboxAttestation:
        await self._safe_request(client, "GET", "/ready", accepted={200})
        server_response = await self._safe_request(client, "GET", "/server_info", accepted={200})
        server_info = _json_object(server_response, "OpenHands server info")
        try:
            attestation = await self._attestor.attest(job, server_info)
        except OpenHandsRemoteWorkerError as exc:
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                str(exc),
                False,
            ) from exc
        self._validate_attestation(job, attestation)
        return attestation

    async def _preflight(self, client: httpx.AsyncClient, job: CodingJob) -> None:
        attestation = await self._ready_and_attest(client, job)
        workspace = attestation.workspace_root
        commits = await self._get_json(
            client,
            "/api/git/commits",
            params={"path": workspace, "limit": "1"},
            context="OpenHands repository HEAD",
        )
        if not isinstance(commits.get("commits"), list) or not commits["commits"]:
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands sandbox does not expose the pinned repository HEAD",
                False,
            )
        first = commits["commits"][0]
        if (
            not isinstance(first, dict)
            or str(first.get("sha", "")).lower() != job.repository.base_sha.lower()
        ):
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands sandbox HEAD does not match the pinned base SHA",
                False,
            )
        changes = await self._git_changes(client, workspace, job.repository.base_sha)
        if changes:
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands sandbox must start from a clean pinned repository",
                False,
            )

    def _validate_attestation(
        self,
        job: CodingJob,
        attestation: OpenHandsSandboxAttestation,
    ) -> None:
        expected_workspace = job.lease.workspace_root.as_posix()
        expected_allowed = tuple(job.allowed_paths.roots)
        checks = (
            (attestation.lease_id == job.lease.lease_id, "lease identity"),
            (attestation.repository_id == job.repository.repository_id, "repository identity"),
            (attestation.base_sha.lower() == job.repository.base_sha.lower(), "base SHA"),
            (attestation.tree_digest == job.repository.tree_digest, "repository tree digest"),
            (attestation.workspace_root == expected_workspace, "workspace root"),
            (attestation.isolation_class is job.lease.isolation_class, "isolation class"),
            (attestation.expires_at == job.lease.expires_at, "lease expiry"),
            (attestation.allowed_paths == expected_allowed, "allowed paths"),
            (attestation.process_policy == job.process_policy, "process policy"),
            (attestation.network_policy == job.network_policy, "network policy"),
            (attestation.resource_budget == job.resource_budget, "resource budget"),
            (attestation.permission_ceiling == job.permission_ceiling, "permission ceiling"),
        )
        for matched, label in checks:
            if not matched:
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    f"trusted OpenHands sandbox attestation mismatches job {label}",
                    False,
                )

    async def _create_or_resume_conversation(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        conversation_id: uuid.UUID,
    ) -> None:
        payload = {
            "conversation_id": str(conversation_id),
            "agent_profile_id": self._config.agent_profile_id,
            "workspace": {"working_dir": job.lease.workspace_root.as_posix()},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": _job_prompt(job)}],
                "run": True,
            },
            "max_iterations": self._config.max_iterations,
            "autotitle": False,
        }
        response = await self._safe_request(
            client,
            "POST",
            "/api/conversations",
            accepted={200, 201, 409},
            json=payload,
        )
        if response.status_code == 409:
            existing = await self._get_conversation(client, conversation_id)
            self._validate_conversation_identity(existing, job, conversation_id)
            return
        body = _json_object(response, "OpenHands conversation creation")
        self._validate_conversation_identity(body, job, conversation_id)

    async def _get_conversation(
        self,
        client: httpx.AsyncClient,
        conversation_id: uuid.UUID,
    ) -> dict[str, Any]:
        response = await self._safe_request(
            client,
            "GET",
            f"/api/conversations/{conversation_id}",
            accepted={200, 404},
        )
        if response.status_code == 404:
            raise _RemoteProblem(
                WorkerFailureKind.INTERNAL_ERROR,
                "OpenHands recovery conversation is missing",
                False,
            )
        return _json_object(response, "OpenHands conversation")

    def _validate_conversation_identity(
        self,
        body: Mapping[str, object],
        job: CodingJob,
        conversation_id: uuid.UUID,
    ) -> None:
        observed_id = str(body.get("id") or body.get("conversation_id") or "")
        if observed_id and observed_id != str(conversation_id):
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands returned a different conversation identity",
                False,
            )
        workspace = body.get("workspace")
        if isinstance(workspace, dict):
            observed_root = workspace.get("working_dir")
            if (
                observed_root is not None
                and str(observed_root) != job.lease.workspace_root.as_posix()
            ):
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    "OpenHands conversation workspace identity changed",
                    False,
                )

    async def _poll_until_stopped(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        conversation_id: uuid.UUID,
        deadline: float,
    ) -> str:
        while self._clock() < deadline:
            body = await self._get_conversation(client, conversation_id)
            self._validate_conversation_identity(body, job, conversation_id)
            status = _conversation_status(body)
            if status in _TERMINAL_STATUSES or status in _PAUSED_STATUSES:
                return status
            await self._sleep(self._config.poll_interval_seconds)

        await self._safe_request(
            client,
            "POST",
            f"/api/conversations/{conversation_id}/interrupt",
            accepted={200, 404, 409},
        )
        raise _RemoteProblem(
            WorkerFailureKind.TIMEOUT,
            "OpenHands coding run exceeded the declared resource budget",
            True,
        )

    async def _result_for_status(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        conversation_id: uuid.UUID,
        status: str,
        deadline: float,
    ) -> CodingResult:
        state = RecoveryState(status, str(conversation_id))
        if status == "finished":
            result = await self._collect_success(client, job, deadline)
            return CodingResult(
                job_id=result.job_id,
                changed_files=result.changed_files,
                test_evidence=result.test_evidence,
                artifacts=result.artifacts,
                recovery_state=state,
                failure=result.failure,
            )
        if status == "paused":
            return CodingResult(
                job_id=job.job_id,
                recovery_state=state,
                failure=WorkerFailure(
                    WorkerFailureKind.CANCELLED,
                    "OpenHands conversation is paused and requires explicit recovery",
                    retryable=True,
                ),
            )
        if status == "waiting_for_confirmation":
            return CodingResult(
                job_id=job.job_id,
                recovery_state=state,
                failure=WorkerFailure(
                    WorkerFailureKind.POLICY_VIOLATION,
                    "OpenHands agent profile requires an interactive confirmation "
                    "outside this backend",
                    retryable=False,
                ),
            )
        return CodingResult(
            job_id=job.job_id,
            recovery_state=state,
            failure=WorkerFailure(
                WorkerFailureKind.PROCESS_FAILED,
                f"OpenHands coding conversation ended with status {status}",
                retryable=status == "stuck",
            ),
        )

    async def _collect_success(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        deadline: float,
    ) -> CodingResult:
        workspace = job.lease.workspace_root.as_posix()
        before_tests = await self._collect_changed_files(client, job, workspace)
        test_evidence, failure = await self._run_acceptance(
            client, job, workspace, deadline
        )
        after_tests = await self._collect_changed_files(client, job, workspace)
        if before_tests != after_tests:
            failure = WorkerFailure(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands acceptance commands mutated the candidate tree",
                retryable=False,
            )
        return CodingResult(
            job_id=job.job_id,
            changed_files=after_tests,
            test_evidence=test_evidence,
            failure=failure,
        )

    async def _run_acceptance(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        workspace: str,
        deadline: float,
    ) -> tuple[tuple[TestEvidence, ...], WorkerFailure | None]:
        evidence: list[TestEvidence] = []
        output_bytes = 0
        for command in job.acceptance_commands:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return tuple(evidence), WorkerFailure(
                    WorkerFailureKind.TIMEOUT,
                    "OpenHands coding job exhausted its resource budget before acceptance",
                    retryable=True,
                )
            typed = validate_typed_argv(command.argv, job.process_policy.allowed_executables)
            cwd = workspace
            if command.cwd != ".":
                relative = normalize_relative_path(command.cwd)
                cwd = f"{workspace.rstrip('/')}/{relative.as_posix()}"
            timeout = min(
                command.timeout_seconds or job.resource_budget.timeout_seconds,
                max(1, int(remaining)),
            )
            response = await self._safe_request(
                client,
                "POST",
                "/api/bash/execute_bash_command",
                accepted={200},
                json={
                    "command": shlex.join(typed),
                    "cwd": cwd,
                    "timeout": timeout,
                },
                timeout=max(self._config.request_timeout_seconds, float(timeout) + 1.0),
            )
            body = _json_object(response, "OpenHands acceptance command")
            exit_code = body.get("exit_code")
            stdout = body.get("stdout") or ""
            stderr = body.get("stderr") or ""
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise _RemoteProblem(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "OpenHands acceptance command returned an invalid exit code",
                    False,
                )
            if not isinstance(stdout, str) or not isinstance(stderr, str):
                raise _RemoteProblem(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "OpenHands acceptance command returned invalid output",
                    False,
                )
            encoded_stdout = stdout.encode("utf-8")
            encoded_stderr = stderr.encode("utf-8")
            output_bytes += len(encoded_stdout) + len(encoded_stderr)
            if output_bytes > job.resource_budget.max_output_bytes:
                return tuple(evidence), WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "OpenHands acceptance output exceeded the declared resource budget",
                    retryable=False,
                )
            digest = hashlib.sha256(encoded_stdout + b"\0" + encoded_stderr).hexdigest()
            evidence.append(TestEvidence(tuple(command.argv), exit_code, digest))
            if exit_code != 0:
                return tuple(evidence), WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "OpenHands acceptance command failed",
                    retryable=True,
                )
        return tuple(evidence), None

    async def _collect_changed_files(
        self,
        client: httpx.AsyncClient,
        job: CodingJob,
        workspace: str,
    ) -> tuple[ChangedFile, ...]:
        changes = await self._git_changes(client, workspace, job.repository.base_sha)
        if len(changes) > job.resource_budget.max_changed_files:
            raise _RemoteProblem(
                WorkerFailureKind.POLICY_VIOLATION,
                "OpenHands worker exceeded the changed-file budget",
                False,
            )

        changed: list[ChangedFile] = []
        seen: set[str] = set()
        for raw in changes:
            if not isinstance(raw, dict):
                raise _RemoteProblem(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "OpenHands git evidence contains an invalid change record",
                    False,
                )
            status = str(raw.get("status", "")).upper()
            raw_path = raw.get("path")
            if not isinstance(raw_path, str):
                raise _RemoteProblem(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "OpenHands git evidence contains an invalid path",
                    False,
                )
            try:
                path = normalize_relative_path(raw_path).as_posix()
            except ValueError as exc:
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    "OpenHands git evidence escaped repository-relative path identity",
                    False,
                ) from exc
            if path in seen:
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    "OpenHands git evidence repeats changed-file identity",
                    False,
                )
            seen.add(path)
            if not job.allowed_paths.allows(path):
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    f"OpenHands changed path outside the declared scope: {path}",
                    False,
                )
            if status not in {"ADDED", "UPDATED"}:
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    f"OpenHands change status {status or 'UNKNOWN'} is not yet evidence-safe",
                    False,
                )
            response = await self._safe_request(
                client,
                "GET",
                "/api/file/download",
                accepted={200},
                params={"path": f"{workspace.rstrip('/')}/{path}"},
                accept_json=False,
            )
            content = response.content
            if len(content) > job.resource_budget.max_output_bytes:
                raise _RemoteProblem(
                    WorkerFailureKind.POLICY_VIOLATION,
                    f"OpenHands changed file exceeds the evidence byte budget: {path}",
                    False,
                )
            changed.append(
                ChangedFile(
                    path=path,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                )
            )
        return tuple(sorted(changed, key=lambda item: item.path))

    async def _git_changes(
        self,
        client: httpx.AsyncClient,
        workspace: str,
        base_sha: str,
    ) -> list[object]:
        response = await self._safe_request(
            client,
            "GET",
            "/api/git/changes",
            accepted={200},
            params={"path": workspace, "ref": base_sha},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise _RemoteProblem(
                WorkerFailureKind.INTERNAL_ERROR,
                "OpenHands git changes response is not valid JSON",
                False,
            ) from exc
        if not isinstance(body, list):
            raise _RemoteProblem(
                WorkerFailureKind.INTERNAL_ERROR,
                "OpenHands git changes response has an invalid schema",
                False,
            )
        return body

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: Mapping[str, str],
        context: str,
    ) -> dict[str, Any]:
        response = await self._safe_request(client, "GET", path, accepted={200}, params=params)
        return _json_object(response, context)

    async def _safe_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        accepted: set[int],
        accept_json: bool = True,
        **kwargs: object,
    ) -> httpx.Response:
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise _RemoteProblem(
                WorkerFailureKind.TIMEOUT,
                "OpenHands request timed out",
                True,
            ) from exc
        except httpx.HTTPError as exc:
            raise _RemoteProblem(
                WorkerFailureKind.INTERNAL_ERROR,
                "OpenHands transport failed",
                True,
            ) from exc
        if response.status_code not in accepted:
            status = response.status_code
            if status in {401, 403}:
                kind = WorkerFailureKind.POLICY_VIOLATION
                retryable = False
            elif status == 429 or status >= 500:
                kind = WorkerFailureKind.INTERNAL_ERROR
                retryable = True
            else:
                kind = WorkerFailureKind.INTERNAL_ERROR
                retryable = False
            raise _RemoteProblem(kind, f"OpenHands returned HTTP {status}", retryable)
        if accept_json and response.status_code != 404 and response.content:
            # Parse once here only to reject malformed responses without exposing bodies.
            try:
                response.json()
            except ValueError as exc:
                raise _RemoteProblem(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "OpenHands returned invalid JSON",
                    False,
                ) from exc
        return response


def _failure_result(
    job: CodingJob,
    problem: _RemoteProblem,
    *,
    recovery_possible: bool = False,
) -> CodingResult:
    conversation_id = _conversation_id(job.job_id)
    recovery_state = (
        RecoveryState("unknown", str(conversation_id))
        if problem.retryable and recovery_possible
        else None
    )
    return CodingResult(
        job_id=job.job_id,
        recovery_state=recovery_state,
        failure=WorkerFailure(problem.kind, problem.message, retryable=problem.retryable),
    )


def _conversation_id(job_id: str) -> uuid.UUID:
    if not job_id.strip():
        raise OpenHandsRemoteWorkerError("job_id must not be empty")
    return uuid.uuid5(_CONVERSATION_NAMESPACE, job_id)


def _conversation_status(body: Mapping[str, object]) -> str:
    value = body.get("execution_status") or body.get("status")
    if not isinstance(value, str) or not value.strip():
        raise _RemoteProblem(
            WorkerFailureKind.INTERNAL_ERROR,
            "OpenHands conversation omitted execution status",
            False,
        )
    return value.strip().lower()


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise _RemoteProblem(
            WorkerFailureKind.INTERNAL_ERROR,
            f"{context} is not valid JSON",
            False,
        ) from exc
    if not isinstance(body, dict):
        raise _RemoteProblem(
            WorkerFailureKind.INTERNAL_ERROR,
            f"{context} has an invalid schema",
            False,
        )
    return body


def _job_prompt(job: CodingJob) -> str:
    allowed = ", ".join(job.allowed_paths.roots)
    permissions = ", ".join(sorted(job.permission_ceiling))
    return (
        "Nika Core delegated coding job.\n"
        f"Goal: {job.goal}\n"
        f"Pinned base SHA: {job.repository.base_sha}\n"
        f"Allowed repository paths: {allowed}\n"
        f"Permission ceiling: {permissions}\n"
        "Work only inside the supplied sandbox workspace and allowed repository paths. "
        "Do not publish, push, open pull requests, or change credentials. Nika will run the "
        "declared acceptance commands independently after your coding run."
    )


def _server_build_sha(server_info: Mapping[str, object]) -> str | None:
    candidates = (
        server_info.get("build_git_sha"),
        server_info.get("build_sha"),
        server_info.get("commit_sha"),
        server_info.get("git_sha"),
    )
    for value in candidates:
        if isinstance(value, str) and value:
            try:
                _validate_git_sha(value, "server build SHA")
            except OpenHandsRemoteWorkerError:
                continue
            return value.lower()
    return None


def _validate_git_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise OpenHandsRemoteWorkerError(f"{label} must be a 40-character hexadecimal SHA")


def _is_loopback_host(host: str) -> bool:
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
