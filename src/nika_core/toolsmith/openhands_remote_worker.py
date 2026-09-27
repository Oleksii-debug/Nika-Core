from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import pathlib
import shutil
import tempfile
import threading
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
    normalize_job_relative_path,
    sterile_git_environment,
)

_LOGGER = logging.getLogger(__name__)


class OpenHandsWorkerError(RuntimeError):
    """Raised when a remote coding run cannot satisfy Nika's worker contract."""


class OpenHandsWorkspaceMutationError(RuntimeError):
    """Raised when a local staging mutation cannot be proven rolled back."""


class OpenHandsSandboxAcquisitionError(RuntimeError):
    """Raised when the injected sandbox provider fails before endpoint validation."""


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
    network_policy_enforced: bool = False
    fresh_workspace: bool = True

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (self.endpoint_id, self.host, self.working_dir)):
            raise ValueError("OpenHands endpoint identity, host and working_dir must be exact strings")
        if type(self.sandbox_egress_hosts) is not tuple or any(
            type(host) is not str for host in self.sandbox_egress_hosts
        ):
            raise ValueError("OpenHands sandbox egress hosts must be an immutable string tuple")
        if type(self.network_policy_enforced) is not bool or type(self.fresh_workspace) is not bool:
            raise ValueError("OpenHands sandbox attestations must use exact booleans")
        if not self.endpoint_id.strip() or not self.host.strip() or not self.working_dir.strip():
            raise ValueError("OpenHands endpoint identity, host and working_dir are required")
        parsed = urlparse(self.host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OpenHands endpoint host must be an absolute http(s) URL")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.params
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "OpenHands endpoint host must contain authority only and no embedded credentials"
            )
        working_dir = pathlib.PurePosixPath(self.working_dir)
        if (
            not self.working_dir.startswith("/")
            or "\\" in self.working_dir
            or ".." in working_dir.parts
            or working_dir == pathlib.PurePosixPath("/")
            or working_dir.as_posix() != self.working_dir
        ):
            raise ValueError(
                "OpenHands remote working_dir must be a canonical absolute POSIX directory"
            )
        if self.isolation_class is not IsolationClass.REMOTE_SANDBOXED:
            raise ValueError("OpenHands endpoint must attest REMOTE_SANDBOXED isolation")
        if not self.fresh_workspace:
            raise ValueError("OpenHands endpoint must provide a fresh per-job workspace")
        if not self.network_policy_enforced:
            raise ValueError("OpenHands endpoint must attest enforced sandbox network policy")
        if any(not host.strip() for host in self.sandbox_egress_hosts):
            raise ValueError("OpenHands sandbox egress hosts must not be empty")

    @property
    def control_plane_host(self) -> str:
        parsed = urlparse(self.host)
        assert parsed.hostname is not None
        return parsed.hostname.casefold()

    @property
    def control_plane_scheme(self) -> str:
        return urlparse(self.host).scheme.casefold()


@dataclasses.dataclass(frozen=True, slots=True)
class RemoteFile:
    path: str
    data: bytes

    def __post_init__(self) -> None:
        if type(self.path) is not str or type(self.data) is not bytes:
            raise ValueError("OpenHands remote files require exact immutable path/data carriers")


@dataclasses.dataclass(frozen=True, slots=True)
class OpenHandsRunEvidence:
    conversation_id: str
    files: tuple[RemoteFile, ...]

    def __post_init__(self) -> None:
        if type(self.conversation_id) is not str:
            raise ValueError("OpenHands run evidence conversation id must be an exact string")
        if type(self.files) is not tuple or any(type(item) is not RemoteFile for item in self.files):
            raise ValueError("OpenHands run evidence files must be an immutable RemoteFile tuple")
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
class OpenHandsRecoveryStateProbePort(Protocol):
    """Host-owned durable/reconstructable state lookup used after process restart."""

    async def inspect(self, job_id: str) -> RecoveryState | None: ...


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

    async def cancel(self, job_id: str) -> bool: ...


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
        *,
        recovery_probe: OpenHandsRecoveryStateProbePort | None = None,
    ) -> None:
        self._sandbox_provider = sandbox_provider
        self._runtime = runtime
        self._recovery_probe = recovery_probe
        self._states: dict[str, RecoveryState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._finalized_results: dict[str, CodingResult] = {}
        self._runtime_inflight: set[str] = set()
        self._acceptance_inflight: set[str] = set()
        self._lock = asyncio.Lock()

    async def execute(self, job: CodingJob) -> CodingResult:
        async with self._lock:
            existing = self._states.get(job.job_id)
            if existing is not None:
                return _failure_result(
                    job,
                    WorkerFailureKind.INVALID_REQUEST,
                    "coding job identity already has worker state; use recovery or a new job",
                    retryable=False,
                    state=existing,
                )
            cancel_event = threading.Event()
            self._states[job.job_id] = RecoveryState("running")
            self._cancel_events[job.job_id] = cancel_event

        endpoint: OpenHandsSandboxEndpoint | None = None
        local_root: pathlib.Path | None = None
        changed: tuple[ChangedFile, ...] = ()
        tests: tuple[TestEvidence, ...] = ()
        applied = False
        task_cancelled = False
        sandbox_acquisition_unresolved = False
        result: CodingResult
        try:
            try:
                local_root = _validate_local_workspace(job)
                source_evidence = collect_tree_evidence(local_root)
                _validate_source_identity(job, source_evidence)
                sandbox_acquisition_unresolved = True
                try:
                    endpoint = await self._sandbox_provider.acquire(job)
                    sandbox_acquisition_unresolved = False
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - sandbox provider boundary
                    _LOGGER.error(
                        "OpenHands sandbox acquisition failed (%s)",
                        type(exc).__name__,
                    )
                    raise OpenHandsSandboxAcquisitionError(
                        "remote sandbox acquisition failed"
                    ) from None
                _validate_endpoint(job, endpoint)
                prompt = _build_prompt(job)
                async with self._lock:
                    current = self._states.get(job.job_id)
                    dispatch_remote = current is not None and current.phase == "running"
                    if dispatch_remote:
                        self._runtime_inflight.add(job.job_id)

                if not dispatch_remote:
                    cancelled = await self._current_cancellation_result(
                        job,
                        changed_files=changed,
                        test_evidence=tests,
                    )
                    if cancelled is None:
                        raise OpenHandsWorkerError(
                            "coding job state changed before remote dispatch"
                        )
                    result = cancelled
                else:
                    try:
                        run = await asyncio.wait_for(
                            self._runtime.execute(
                                job,
                                endpoint,
                                prompt,
                                local_root,
                                source_evidence,
                            ),
                            timeout=job.resource_budget.timeout_seconds,
                        )
                        if type(run) is not OpenHandsRunEvidence:
                            raise OpenHandsWorkerError(
                                "remote runtime returned non-canonical run evidence"
                            )
                    finally:
                        # Event-loop tasks cannot interleave between the completed await
                        # and this synchronous discard. cancel() therefore never mistakes
                        # an already-finished remote run for an in-flight one.
                        self._runtime_inflight.discard(job.job_id)

                    cancelled = await self._current_cancellation_result(
                        job,
                        changed_files=changed,
                        test_evidence=tests,
                    )
                    if cancelled is not None:
                        result = cancelled
                    else:
                        changed = _validate_and_apply_snapshot(
                            job,
                            local_root,
                            source_evidence,
                            run.files,
                        )
                        applied = True
                        candidate_evidence = collect_tree_evidence(local_root)
                        tests = await self._run_guarded_acceptance(
                            job,
                            local_root,
                            candidate_evidence,
                            cancel_event,
                        )
                        post_acceptance_evidence = collect_tree_evidence(local_root)
                        if post_acceptance_evidence != candidate_evidence:
                            raise OpenHandsWorkspaceMutationError(
                                "acceptance commands mutated the validated candidate"
                            )

                        cancelled = await self._current_cancellation_result(
                            job,
                            changed_files=changed,
                            test_evidence=tests,
                        )
                        if cancelled is not None:
                            result = cancelled
                        else:
                            failed_test = next(
                                (evidence for evidence in tests if evidence.exit_code != 0),
                                None,
                            )
                            if failed_test is not None:
                                state = RecoveryState("repair_required", run.conversation_id)
                                result = CodingResult(
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
                            else:
                                state = RecoveryState("completed", run.conversation_id)
                                result = CodingResult(
                                    job_id=job.job_id,
                                    changed_files=changed,
                                    test_evidence=tests,
                                    recovery_state=state,
                                )
            except TimeoutError:
                cancel_event.set()
                stopped = await self._runtime.cancel(job.job_id)
                state = RecoveryState(
                    "interrupted" if stopped and not applied else "manual_reconcile_required"
                )
                result = _failure_result(
                    job,
                    WorkerFailureKind.TIMEOUT,
                    "remote coding worker exceeded its Nika resource deadline",
                    retryable=stopped and not applied,
                    state=state,
                    changed_files=changed,
                    test_evidence=tests,
                )
            except asyncio.CancelledError:
                task_cancelled = True
                cancel_event.set()
                stopped = await self._runtime.cancel(job.job_id)
                state = RecoveryState(
                    "cancelled"
                    if stopped and not applied and not sandbox_acquisition_unresolved
                    else "manual_reconcile_required"
                )
                if state.phase == "cancelled":
                    result = _failure_result(
                        job,
                        WorkerFailureKind.CANCELLED,
                        "coding job cancellation was confirmed; cancelled work is terminal",
                        retryable=False,
                        state=state,
                        changed_files=changed,
                        test_evidence=tests,
                    )
                else:
                    result = _failure_result(
                        job,
                        WorkerFailureKind.INTERNAL_ERROR,
                        "coding job stop could not be proven; host reconciliation is required",
                        retryable=False,
                        state=state,
                        changed_files=changed,
                        test_evidence=tests,
                    )
            except OpenHandsSandboxAcquisitionError:
                state = RecoveryState("manual_reconcile_required")
                result = _failure_result(
                    job,
                    WorkerFailureKind.INTERNAL_ERROR,
                    "remote sandbox acquisition failed without trusted diagnostics",
                    retryable=False,
                    state=state,
                    changed_files=changed,
                    test_evidence=tests,
                )
            except OpenHandsWorkspaceMutationError:
                state = RecoveryState("manual_reconcile_required")
                result = _failure_result(
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
                result = _failure_result(
                    job,
                    WorkerFailureKind.POLICY_VIOLATION,
                    _safe_message(exc, "remote coding result violated Nika policy"),
                    retryable=False,
                    state=state,
                    changed_files=changed,
                    test_evidence=tests,
                )
            except Exception:  # noqa: BLE001 - untrusted coding-engine boundary
                state = RecoveryState(
                    "manual_reconcile_required" if applied else "interrupted"
                )
                result = _failure_result(
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
                release_succeeded = result.succeeded if "result" in locals() else False
                async with self._lock:
                    current = self._states.get(job.job_id)
                    if current is not None and current.phase != "running":
                        release_succeeded = False
                try:
                    await self._sandbox_provider.release(
                        job,
                        endpoint,
                        succeeded=release_succeeded,
                    )
                except Exception as exc:  # noqa: BLE001 - sandbox provider boundary
                    _LOGGER.error(
                        "OpenHands sandbox release requires provider reconciliation (%s)",
                        type(exc).__name__,
                    )
                    result = _failure_result(
                        job,
                        WorkerFailureKind.INTERNAL_ERROR,
                        "remote sandbox cleanup could not be proven",
                        retryable=False,
                        state=RecoveryState("manual_reconcile_required"),
                        changed_files=changed,
                        test_evidence=tests,
                    )

        result = await self._finalize_result(job, result)
        if task_cancelled:
            raise asyncio.CancelledError
        return result

    async def cancel(self, job_id: str) -> None:
        wait_for_acceptance = False
        async with self._lock:
            current = self._states.get(job_id)
            if current is not None and current.phase not in {
                "running",
                "cancel_requested",
                "acceptance_cancel_requested",
            }:
                return

            if current is None:
                # Missing process-local state is not evidence that no remote execution
                # exists. After restart, durable Product Factory state may still prove
                # that this identity crossed the dispatch boundary. Never ask a fresh
                # runtime instance to manufacture a stop proof for that lost execution.
                self._states[job_id] = RecoveryState("cancel_probe_pending")
                unknown_process_state = True
            else:
                unknown_process_state = False
                runtime_inflight = job_id in self._runtime_inflight
                acceptance_inflight = job_id in self._acceptance_inflight
                if acceptance_inflight and not runtime_inflight:
                    self._states[job_id] = RecoveryState("acceptance_cancel_requested")
                    wait_for_acceptance = True
                else:
                    self._states[job_id] = RecoveryState("cancel_requested")

            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()

            # Before any remote or acceptance execution, or after both have returned,
            # there is no external process left to stop. Only then is an immediate
            # terminal cancellation proof valid.
            if (
                not unknown_process_state
                and current is not None
                and job_id not in self._runtime_inflight
                and job_id not in self._acceptance_inflight
                and not wait_for_acceptance
            ):
                self._states[job_id] = RecoveryState("cancelled")
                return

        if unknown_process_state:
            try:
                durable_state = (
                    await self._recovery_probe.inspect(job_id)
                    if self._recovery_probe is not None
                    else None
                )
            except asyncio.CancelledError:
                async with self._lock:
                    current = self._states.get(job_id)
                    if current is not None and current.phase == "cancel_probe_pending":
                        self._states[job_id] = RecoveryState("manual_reconcile_required")
                raise
            except Exception as exc:  # noqa: BLE001 - host recovery probe boundary
                _LOGGER.error(
                    "OpenHands durable cancellation probe failed (%s)",
                    type(exc).__name__,
                )
                durable_state = None

            async with self._lock:
                current = self._states.get(job_id)
                if current is None or current.phase != "cancel_probe_pending":
                    return
                if durable_state is not None and durable_state.phase == "cancelled":
                    self._states[job_id] = durable_state
                else:
                    self._states[job_id] = RecoveryState(
                        "manual_reconcile_required",
                        durable_state.opaque_token if durable_state is not None else None,
                    )
            return

        if wait_for_acceptance:
            for _ in range(100):
                async with self._lock:
                    current = self._states.get(job_id)
                    if current is None or current.phase != "acceptance_cancel_requested":
                        return
                await asyncio.sleep(0.05)
            async with self._lock:
                current = self._states.get(job_id)
                if current is not None and current.phase == "acceptance_cancel_requested":
                    self._states[job_id] = RecoveryState("manual_reconcile_required")
            return

        stopped = await self._runtime.cancel(job_id)
        async with self._lock:
            current = self._states.get(job_id)
            if current is None or current.phase != "cancel_requested":
                return
            self._states[job_id] = RecoveryState(
                "cancelled" if stopped else "manual_reconcile_required"
            )

    async def inspect(self, job_id: str) -> RecoveryState | None:
        async with self._lock:
            state = self._states.get(job_id)
        if state is not None:
            return state
        if self._recovery_probe is None:
            return None
        return await self._recovery_probe.inspect(job_id)

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        async with self._lock:
            finalized = self._finalized_results.get(job.job_id)
        if finalized is not None and finalized.recovery_state == state:
            return finalized

        if state.phase == "completed":
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "completed worker result is unavailable; host reconciliation is required",
                retryable=False,
                state=RecoveryState("manual_reconcile_required"),
            )
        if state.phase == "cancelled":
            return _failure_result(
                job,
                WorkerFailureKind.CANCELLED,
                "coding job cancellation was confirmed; cancelled work is terminal",
                retryable=False,
                state=state,
            )
        if state.phase in {
            "cancel_requested",
            "cancel_probe_pending",
            "manual_reconcile_required",
        }:
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "coding job stop could not be proven; host reconciliation is required",
                retryable=False,
                state=RecoveryState("manual_reconcile_required", state.opaque_token),
            )
        if state.phase != "interrupted":
            return _failure_result(
                job,
                WorkerFailureKind.INVALID_REQUEST,
                "recovery state is not restartable",
                retryable=False,
                state=state,
            )

        async with self._lock:
            current = self._states.get(job.job_id)
        if current is None:
            if self._recovery_probe is None:
                return _failure_result(
                    job,
                    WorkerFailureKind.INTERNAL_ERROR,
                    "interrupted worker identity is not durably reconstructable; host reconciliation is required",
                    retryable=False,
                    state=RecoveryState("manual_reconcile_required"),
                )
            durable_state = await self._recovery_probe.inspect(job.job_id)
            if durable_state != state:
                return _failure_result(
                    job,
                    WorkerFailureKind.INVALID_REQUEST,
                    "recovery state does not match durable worker identity",
                    retryable=False,
                    state=durable_state or RecoveryState("manual_reconcile_required"),
                )

        async with self._lock:
            current = self._states.get(job.job_id)
            if current is not None and current != state:
                return _failure_result(
                    job,
                    WorkerFailureKind.INVALID_REQUEST,
                    "recovery state is stale for the current worker identity",
                    retryable=False,
                    state=current,
                )
            self._states.pop(job.job_id, None)
            self._cancel_events.pop(job.job_id, None)
            self._finalized_results.pop(job.job_id, None)
        return await self.execute(job)

    async def _run_guarded_acceptance(
        self,
        job: CodingJob,
        root: pathlib.Path,
        candidate_evidence: TreeEvidence,
        cancellation_event: threading.Event,
    ) -> tuple[TestEvidence, ...]:
        async with self._lock:
            current = self._states.get(job.job_id)
            if current is None or current.phase != "running":
                return ()
            self._acceptance_inflight.add(job.job_id)

        acceptance_stopped = False
        try:
            evidence = await _run_acceptance_async(
                job,
                root,
                candidate_evidence,
                cancellation_event,
            )
            acceptance_stopped = True
            return evidence
        finally:
            async with self._lock:
                self._acceptance_inflight.discard(job.job_id)
                current = self._states.get(job.job_id)
                if current is not None and current.phase == "acceptance_cancel_requested":
                    self._states[job.job_id] = RecoveryState(
                        "cancelled" if acceptance_stopped else "manual_reconcile_required",
                        current.opaque_token,
                    )

    async def _current_cancellation_result(
        self,
        job: CodingJob,
        *,
        changed_files: tuple[ChangedFile, ...],
        test_evidence: tuple[TestEvidence, ...],
    ) -> CodingResult | None:
        async with self._lock:
            state = self._states.get(job.job_id)
        if state is None or state.phase == "running":
            return None
        if state.phase in {"cancel_requested", "acceptance_cancel_requested"}:
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "coding job cancellation is pending; remote stop is not yet proven",
                retryable=False,
                state=RecoveryState("manual_reconcile_required", state.opaque_token),
                changed_files=changed_files,
                test_evidence=test_evidence,
            )
        if state.phase == "cancelled":
            return _failure_result(
                job,
                WorkerFailureKind.CANCELLED,
                "coding job cancellation was confirmed; cancelled work is terminal",
                retryable=False,
                state=state,
                changed_files=changed_files,
                test_evidence=test_evidence,
            )
        if state.phase == "manual_reconcile_required":
            return _failure_result(
                job,
                WorkerFailureKind.INTERNAL_ERROR,
                "coding job stop could not be proven; host reconciliation is required",
                retryable=False,
                state=state,
                changed_files=changed_files,
                test_evidence=test_evidence,
            )
        return None

    async def _set_state(self, job_id: str, state: RecoveryState) -> None:
        """Test/recovery seam that preserves the worker's state/event invariants."""

        async with self._lock:
            self._states[job_id] = state
            if state.phase == "running":
                self._cancel_events.setdefault(job_id, threading.Event())
            elif state.phase not in {"cancel_requested", "acceptance_cancel_requested"}:
                self._cancel_events.pop(job_id, None)

    async def _finalize_result(self, job: CodingJob, result: CodingResult) -> CodingResult:
        async with self._lock:
            current = self._states.get(job.job_id)
            result_state = result.recovery_state
            if result_state is None:
                result_state = RecoveryState("manual_reconcile_required")
                result = dataclasses.replace(result, recovery_state=result_state)

            if result_state.phase != "manual_reconcile_required" and current is not None:
                if current.phase == "cancelled":
                    result_state = current
                    result = _failure_result(
                        job,
                        WorkerFailureKind.CANCELLED,
                        "coding job cancellation was confirmed; cancelled work is terminal",
                        retryable=False,
                        state=result_state,
                        changed_files=result.changed_files,
                        test_evidence=result.test_evidence,
                    )
                elif current.phase in {
                    "cancel_requested",
                    "acceptance_cancel_requested",
                    "cancel_probe_pending",
                    "manual_reconcile_required",
                }:
                    result_state = RecoveryState(
                        "manual_reconcile_required",
                        current.opaque_token,
                    )
                    result = _failure_result(
                        job,
                        WorkerFailureKind.INTERNAL_ERROR,
                        "coding job stop could not be proven; host reconciliation is required",
                        retryable=False,
                        state=result_state,
                        changed_files=result.changed_files,
                        test_evidence=result.test_evidence,
                    )

            self._states[job.job_id] = result_state
            self._cancel_events.pop(job.job_id, None)
            self._finalized_results[job.job_id] = result
            return result


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
        expires = datetime.fromisoformat(job.lease.expires_at)
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
    if type(endpoint) is not OpenHandsSandboxEndpoint:
        raise OpenHandsWorkerError("sandbox provider returned a non-canonical endpoint attestation")
    if job.network_policy.mode is not NetworkMode.APPROVED_HOSTS:
        raise OpenHandsWorkerError("remote coding requires explicit approved-host network policy")
    approved = {_normalize_host(host) for host in job.network_policy.approved_hosts}
    required = {endpoint.control_plane_host}
    required.update(_normalize_host(host) for host in endpoint.sandbox_egress_hosts)
    if not required <= approved:
        raise OpenHandsWorkerError("remote coding endpoint or sandbox egress is not approved")
    loopback = endpoint.control_plane_host in {"127.0.0.1", "localhost"}
    if not loopback and endpoint.control_plane_scheme != "https":
        raise OpenHandsWorkerError("non-loopback OpenHands control plane requires HTTPS")


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
    current_before = collect_tree_evidence(local_root)
    if current_before != before:
        raise OpenHandsWorkerError(
            "local staging workspace changed during remote coding execution"
        )

    before_map = {item.path: item for item in before.files}
    before_casefold: dict[str, str] = {}
    for path in before_map:
        folded = path.casefold()
        previous = before_casefold.get(folded)
        if previous is not None and previous != path:
            raise OpenHandsWorkerError(
                "local staging tree contains Windows-ambiguous case-colliding paths"
            )
        before_casefold[folded] = path

    staged: dict[str, RemoteFile] = {}
    staged_casefold: dict[str, str] = {}
    total_bytes = 0
    for item in remote_files:
        try:
            path = normalize_job_relative_path(item.path).as_posix()
        except WorkspaceSecurityError as exc:
            raise OpenHandsWorkerError("remote snapshot contains an unsafe file path") from exc
        if path != item.path:
            raise OpenHandsWorkerError(
                "remote snapshot file path is not in canonical POSIX spelling"
            )
        if path in staged:
            raise OpenHandsWorkerError("remote snapshot contains duplicate file paths")
        folded = path.casefold()
        previous = staged_casefold.get(folded)
        if previous is not None and previous != path:
            raise OpenHandsWorkerError(
                "remote snapshot contains Windows-ambiguous case-colliding file paths"
            )
        staged_casefold[folded] = path
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
            raise OpenHandsWorkerError(
                "local post-apply evidence differs from validated remote delta"
            )
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
        if not rollback_failed:
            try:
                rollback_failed = collect_tree_evidence(local_root) != before
            except (OSError, WorkspaceSecurityError):
                rollback_failed = True
        if rollback_failed:
            raise OpenHandsWorkspaceMutationError(
                "local staging mutation could not be proven rolled back"
            ) from None
        raise

    return tuple(
        ChangedFile(path, after_map[path].sha256, after_map[path].size_bytes)
        for path in changed_paths
    )


async def _run_acceptance_async(
    job: CodingJob,
    root: pathlib.Path,
    candidate_evidence: TreeEvidence,
    cancellation_event: threading.Event,
) -> tuple[TestEvidence, ...]:
    task = asyncio.create_task(
        asyncio.to_thread(
            _run_acceptance,
            job,
            root,
            candidate_evidence,
            cancellation_event,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancellation_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except TimeoutError:
            _LOGGER.warning("acceptance process stop could not be proven before cancellation")
        except Exception as exc:  # noqa: BLE001 - process cleanup after task cancellation
            _LOGGER.warning(
                "acceptance cleanup raised after cancellation (%s)",
                type(exc).__name__,
            )
        raise


def _run_acceptance(
    job: CodingJob,
    root: pathlib.Path,
    candidate_evidence: TreeEvidence,
    cancellation_event: threading.Event,
) -> tuple[TestEvidence, ...]:
    with tempfile.TemporaryDirectory(prefix="nika-openhands-acceptance-") as temp_root_text:
        acceptance_root = pathlib.Path(temp_root_text) / "candidate"
        shutil.copytree(root, acceptance_root, copy_function=shutil.copy2)
        copied_evidence = collect_tree_evidence(acceptance_root)
        if copied_evidence != candidate_evidence:
            raise OpenHandsWorkspaceMutationError(
                "acceptance workspace copy does not match validated candidate"
            )

        evidence: list[TestEvidence] = []
        environment = sterile_git_environment(os.environ)
        for command in job.acceptance_commands:
            if cancellation_event.is_set():
                break
            cwd = acceptance_root
            if command.cwd != ".":
                policy = WorkspacePathPolicy((command.cwd,))
                cwd = ensure_path_policy(
                    acceptance_root,
                    command.cwd,
                    policy,
                    must_exist=True,
                )
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
                cancellation_event=cancellation_event,
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
