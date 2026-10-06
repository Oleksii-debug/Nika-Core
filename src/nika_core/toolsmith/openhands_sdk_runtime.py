from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import logging
import math
import pathlib
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from nika_core.toolsmith.contracts import CodingJob
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRecoveryBinding,
    OpenHandsRecoveryBindingStorePort,
    OpenHandsRunEvidence,
    OpenHandsSandboxEndpoint,
    RemoteFile,
)
from nika_core.toolsmith.workspace_security import (
    TreeEvidence,
    WorkspacePathPolicy,
    ensure_path_policy,
)

_LOGGER = logging.getLogger(__name__)

_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 2000
_MAX_SNAPSHOT_MEMBERS = 10_000
_MAX_HTTP_BLOCK_SECONDS = 10.0
_TERMINAL_FAILURE_STATUSES = frozenset({"error", "stuck"})
_SESSION_HEADER = "X-Session-API-Key"


class OpenHandsAgentServerCompatibilityError(RuntimeError):
    """Raised when the remote Agent Server violates Nika's pinned HTTP contract."""


class OpenHandsAgentServerExecutionCancelled(RuntimeError):
    """Raised in the transport thread when Nika requests cancellation."""


@dataclasses.dataclass(slots=True)
class _ActiveExecution:
    client: httpx.Client | None = None
    conversation_id: str | None = None
    cancel_requested: threading.Event = dataclasses.field(default_factory=threading.Event)
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    effect_possible: bool = False
    stop_proven: bool = False


class OpenHandsAgentServerRuntime:
    """Authenticated HTTP adapter for a separately provisioned OpenHands Agent Server.

    OpenHands and its FastMCP/MCP dependency closure live in the remote server
    environment, never in the Nika process. The client factory owns session-key
    redemption and returns an already-authenticated client. The profile factory
    selects a reviewed server-side Agent Profile, so Nika never serializes model
    credentials or a caller-supplied agent configuration.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[OpenHandsSandboxEndpoint], httpx.Client],
        agent_profile_id_factory: Callable[[CodingJob, OpenHandsSandboxEndpoint], str],
        recovery_binding_store: OpenHandsRecoveryBindingStorePort | None = None,
        max_iterations: int = 96,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
        ):
            raise ValueError("OpenHands max_iterations must be a positive integer")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
            or poll_interval_seconds > 5
        ):
            raise ValueError("OpenHands poll interval must be within (0, 5] seconds")
        self._client_factory = client_factory
        self._agent_profile_id_factory = agent_profile_id_factory
        self._recovery_binding_store = recovery_binding_store
        self._max_iterations = max_iterations
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._active: dict[str, _ActiveExecution] = {}
        self._cancelled_done: set[str] = set()
        self._stopped_done: set[str] = set()
        self._ambiguous_effects: set[str] = set()
        self._pending_cancel: set[str] = set()
        self._active_lock = threading.Lock()

    async def execute(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        prompt: str,
        source_root: pathlib.Path,
        source_evidence: TreeEvidence,
    ) -> OpenHandsRunEvidence:
        active = _ActiveExecution()
        with self._active_lock:
            if job.job_id in self._active:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands Agent Server job identity is already active"
                )
            if job.job_id in self._ambiguous_effects:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands Agent Server prior effect remains unresolved"
                )
            if job.job_id in self._pending_cancel:
                self._pending_cancel.remove(job.job_id)
                active.cancel_requested.set()
            self._cancelled_done.discard(job.job_id)
            self._stopped_done.discard(job.job_id)
            self._active[job.job_id] = active
        try:
            return await asyncio.to_thread(
                self._execute_sync,
                job,
                endpoint,
                prompt,
                source_root,
                source_evidence,
                active,
            )
        except asyncio.CancelledError:
            await self.cancel(job.job_id)
            if not active.done.is_set():
                await asyncio.to_thread(active.done.wait)
            raise

    async def reconcile(
        self,
        job: CodingJob,
        binding: OpenHandsRecoveryBinding,
        source_evidence: TreeEvidence,
    ) -> OpenHandsRunEvidence:
        if type(binding) is not OpenHandsRecoveryBinding or binding.job_id != job.job_id:
            raise OpenHandsAgentServerCompatibilityError(
                "OpenHands recovery binding does not match the coding job"
            )

        active = _ActiveExecution(
            conversation_id=binding.conversation_id,
            effect_possible=True,
        )
        with self._active_lock:
            if job.job_id in self._active:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands Agent Server job identity is already active"
                )
            if job.job_id in self._pending_cancel:
                self._pending_cancel.remove(job.job_id)
                active.cancel_requested.set()
            self._cancelled_done.discard(job.job_id)
            self._stopped_done.discard(job.job_id)
            self._ambiguous_effects.discard(job.job_id)
            self._active[job.job_id] = active

        try:
            return await asyncio.to_thread(
                self._reconcile_sync,
                job,
                binding,
                source_evidence,
                active,
            )
        except asyncio.CancelledError:
            await self.cancel(job.job_id)
            if not active.done.is_set():
                await asyncio.to_thread(active.done.wait)
            raise

    def _reconcile_sync(
        self,
        job: CodingJob,
        binding: OpenHandsRecoveryBinding,
        source_evidence: TreeEvidence,
        active: _ActiveExecution,
    ) -> OpenHandsRunEvidence:
        endpoint = binding.endpoint
        client: httpx.Client | None = None
        try:
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands recovery cancelled before HTTP client acquisition"
                )
            client = self._client_factory(endpoint)
            self._validate_client(client, endpoint)
            with self._active_lock:
                active.client = client

            profile_id = _canonical_uuid(
                self._agent_profile_id_factory(job, endpoint),
                field="agent profile id",
            )
            if profile_id != binding.agent_profile_id:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands recovery profile identity changed"
                )
            expected_conversation_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"nika-core:openhands:{endpoint.endpoint_id}:{job.job_id}",
                )
            )
            if binding.conversation_id != expected_conversation_id:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands recovery conversation identity changed"
                )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands recovery cancelled before status inspection"
                )

            self._wait_for_completion(
                client,
                binding.conversation_id,
                active,
                timeout_seconds=job.resource_budget.timeout_seconds,
            )
            files = _download_snapshot(
                client,
                endpoint,
                baseline_paths={item.path for item in source_evidence.files},
                timeout_seconds=job.resource_budget.timeout_seconds,
                cancellation_event=active.cancel_requested,
            )
            return OpenHandsRunEvidence(binding.conversation_id, files)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - transport cleanup boundary
                    _LOGGER.warning(
                        "OpenHands recovery client close failed (%s)",
                        type(exc).__name__,
                    )
            active.done.set()
            with self._active_lock:
                if active.effect_possible and not active.stop_proven:
                    self._ambiguous_effects.add(job.job_id)
                else:
                    self._ambiguous_effects.discard(job.job_id)
                    if active.cancel_requested.is_set():
                        self._cancelled_done.add(job.job_id)
                    else:
                        self._stopped_done.add(job.job_id)
                if self._active.get(job.job_id) is active:
                    self._active.pop(job.job_id, None)

    async def cancel_recovery(self, binding: OpenHandsRecoveryBinding) -> bool:
        if type(binding) is not OpenHandsRecoveryBinding:
            raise OpenHandsAgentServerCompatibilityError(
                "OpenHands restart cancellation requires an exact recovery binding"
            )
        with self._active_lock:
            if binding.job_id in self._active:
                return False
        return await asyncio.to_thread(self._cancel_recovery_sync, binding)

    def _cancel_recovery_sync(self, binding: OpenHandsRecoveryBinding) -> bool:
        client: httpx.Client | None = None
        try:
            client = self._client_factory(binding.endpoint)
            self._validate_client(client, binding.endpoint)
            self._interrupt_sync(client, binding.conversation_id)
            deadline = time.monotonic() + _MAX_HTTP_BLOCK_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                info = self._request_json(
                    client,
                    "GET",
                    f"/api/conversations/{binding.conversation_id}",
                    operation="restart cancellation status",
                    timeout_seconds=remaining,
                )
                if type(info) is not dict:
                    return False
                status = info.get("execution_status")
                if type(status) is not str:
                    return False
                if status in {"paused", "finished", *_TERMINAL_FAILURE_STATUSES}:
                    return True
                if status not in {"idle", "running", "waiting_for_confirmation", "deleting"}:
                    return False
                time.sleep(min(self._poll_interval_seconds, remaining))
        except Exception as exc:  # noqa: BLE001 - remote restart-cancel boundary
            _LOGGER.warning(
                "OpenHands restart cancellation remains unverified (%s)",
                type(exc).__name__,
            )
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - transport cleanup boundary
                    _LOGGER.warning(
                        "OpenHands restart cancellation client close failed (%s)",
                        type(exc).__name__,
                    )

    async def cancel(self, job_id: str) -> bool:
        with self._active_lock:
            active = self._active.get(job_id)
            if active is None:
                if job_id in self._ambiguous_effects:
                    return False
                if job_id in self._cancelled_done:
                    self._cancelled_done.remove(job_id)
                    self._pending_cancel.add(job_id)
                    return True
                if job_id in self._stopped_done:
                    self._stopped_done.remove(job_id)
                    return True
                if job_id in self._pending_cancel:
                    return False
                self._pending_cancel.add(job_id)
                return True
            active.cancel_requested.set()
            client = active.client
            conversation_id = active.conversation_id

        if client is not None and conversation_id is not None:
            try:
                await asyncio.to_thread(self._interrupt_sync, client, conversation_id)
            except Exception as exc:  # noqa: BLE001 - remote transport boundary
                _LOGGER.warning(
                    "OpenHands Agent Server interrupt failed; stop remains unverified (%s)",
                    type(exc).__name__,
                )

        for _ in range(50):
            if active.done.is_set():
                return active.stop_proven or not active.effect_possible
            await asyncio.sleep(0.1)
        return active.done.is_set() and (
            active.stop_proven or not active.effect_possible
        )

    def _execute_sync(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        prompt: str,
        source_root: pathlib.Path,
        source_evidence: TreeEvidence,
        active: _ActiveExecution,
    ) -> OpenHandsRunEvidence:
        client: httpx.Client | None = None
        try:
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before HTTP client acquisition"
                )

            client = self._client_factory(endpoint)
            self._validate_client(client, endpoint)
            with self._active_lock:
                active.client = client
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before profile binding"
                )
            profile_id = _canonical_uuid(
                self._agent_profile_id_factory(job, endpoint),
                field="agent profile id",
            )
            conversation_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"nika-core:openhands:{endpoint.endpoint_id}:{job.job_id}",
                )
            )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before source upload"
                )

            self._upload_source(
                client,
                endpoint,
                source_root,
                source_evidence,
                timeout_seconds=job.resource_budget.timeout_seconds,
                cancellation_event=active.cancel_requested,
            )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before conversation creation"
                )

            created = self._request_json(
                client,
                "POST",
                "/api/conversations",
                operation="conversation creation",
                json={
                    "conversation_id": conversation_id,
                    "agent_profile_id": profile_id,
                    "workspace": {
                        "kind": "LocalWorkspace",
                        "working_dir": endpoint.working_dir,
                    },
                    "max_iterations": self._max_iterations,
                    "stuck_detection": True,
                    "autotitle": False,
                    "secrets": {},
                    "tags": {
                        "nikajob": _bounded_tag(job.job_id),
                        "nikatask": _bounded_tag(job.task_id),
                    },
                },
                timeout_seconds=job.resource_budget.timeout_seconds,
            )
            if type(created) is not dict:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands conversation response must be a JSON object"
                )
            returned_id = _canonical_uuid(
                created.get("id", created.get("conversation_id")),
                field="conversation id",
            )
            if returned_id != conversation_id:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands conversation identity differs from requested identity"
                )
            with self._active_lock:
                active.conversation_id = conversation_id

            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before recovery binding"
                )
            binding_store = self._recovery_binding_store
            if binding_store is not None:
                try:
                    binding = binding_store.bind(
                        job,
                        endpoint,
                        conversation_id,
                        profile_id,
                    )
                except Exception as exc:  # noqa: BLE001 - durable host boundary
                    _LOGGER.error(
                        "OpenHands recovery binding persistence failed (%s)",
                        type(exc).__name__,
                    )
                    raise OpenHandsAgentServerCompatibilityError(
                        "OpenHands durable recovery binding could not be persisted"
                    ) from None
                if (
                    binding.job_id != job.job_id
                    or binding.endpoint != endpoint
                    or binding.conversation_id != conversation_id
                    or binding.agent_profile_id != profile_id
                ):
                    raise OpenHandsAgentServerCompatibilityError(
                        "OpenHands durable recovery binding changed dispatch identity"
                    )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before message dispatch"
                )
            self._request_json(
                client,
                "POST",
                f"/api/conversations/{conversation_id}/events",
                operation="message dispatch",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                    "run": False,
                },
                timeout_seconds=job.resource_budget.timeout_seconds,
            )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before conversation run"
                )
            active.effect_possible = True
            self._request_json(
                client,
                "POST",
                f"/api/conversations/{conversation_id}/run",
                operation="conversation run",
                json={},
                timeout_seconds=job.resource_budget.timeout_seconds,
            )
            self._wait_for_completion(
                client,
                conversation_id,
                active,
                timeout_seconds=job.resource_budget.timeout_seconds,
            )
            if active.cancel_requested.is_set():
                raise OpenHandsAgentServerExecutionCancelled(
                    "OpenHands execution cancelled before snapshot collection"
                )

            files = _download_snapshot(
                client,
                endpoint,
                baseline_paths={item.path for item in source_evidence.files},
                timeout_seconds=job.resource_budget.timeout_seconds,
                cancellation_event=active.cancel_requested,
            )
            return OpenHandsRunEvidence(conversation_id, files)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - transport cleanup boundary
                    _LOGGER.warning(
                        "OpenHands Agent Server client close failed (%s)",
                        type(exc).__name__,
                    )
            active.done.set()
            with self._active_lock:
                if active.effect_possible and not active.stop_proven:
                    self._ambiguous_effects.add(job.job_id)
                else:
                    self._ambiguous_effects.discard(job.job_id)
                    if active.cancel_requested.is_set():
                        self._cancelled_done.add(job.job_id)
                    else:
                        self._stopped_done.add(job.job_id)
                if self._active.get(job.job_id) is active:
                    self._active.pop(job.job_id, None)

    def _wait_for_completion(
        self,
        client: httpx.Client,
        conversation_id: str,
        active: _ActiveExecution,
        *,
        timeout_seconds: int,
    ) -> None:
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            cancel_requested = active.cancel_requested.is_set()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            info = self._request_json(
                client,
                "GET",
                f"/api/conversations/{conversation_id}",
                operation="conversation status",
                timeout_seconds=remaining,
            )
            # Cancellation can arrive while the blocking status request is in flight.
            # Re-sample before interpreting terminal/paused evidence so fresh stop proof
            # is not discarded by a stale pre-request cancellation snapshot.
            cancel_requested = active.cancel_requested.is_set()
            if type(info) is not dict:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands conversation status must be a JSON object"
                )
            status = info.get("execution_status")
            if type(status) is not str:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands conversation status is missing or non-canonical"
                )
            if status == "finished":
                active.stop_proven = True
                if cancel_requested:
                    raise OpenHandsAgentServerExecutionCancelled(
                        "OpenHands conversation finished after cancellation"
                    )
                return
            if status in _TERMINAL_FAILURE_STATUSES:
                active.stop_proven = True
                raise OpenHandsAgentServerCompatibilityError(
                    f"OpenHands conversation terminated with status {status}"
                )
            if status not in {
                "idle",
                "running",
                "paused",
                "waiting_for_confirmation",
                "deleting",
            }:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands conversation returned an unknown execution status"
                )
            if status in {"paused", "waiting_for_confirmation", "deleting"}:
                if status == "paused" and cancel_requested:
                    active.stop_proven = True
                    raise OpenHandsAgentServerExecutionCancelled(
                        "OpenHands conversation paused after cancellation"
                    )
                raise OpenHandsAgentServerCompatibilityError(
                    f"OpenHands conversation cannot complete unattended from status {status}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(self._poll_interval_seconds, remaining))

    @staticmethod
    def _validate_client(
        client: httpx.Client,
        endpoint: OpenHandsSandboxEndpoint,
    ) -> None:
        if not isinstance(client, httpx.Client):
            raise OpenHandsAgentServerCompatibilityError(
                "OpenHands client factory must return an httpx.Client"
            )
        if str(client.base_url).rstrip("/") != endpoint.host.rstrip("/"):
            raise OpenHandsAgentServerCompatibilityError(
                "OpenHands HTTP client is bound to a different endpoint"
            )
        session_key = client.headers.get(_SESSION_HEADER)
        if type(session_key) is not str or not session_key.strip():
            raise OpenHandsAgentServerCompatibilityError(
                "OpenHands HTTP client lacks session API authentication"
            )

    @staticmethod
    def _upload_source(
        client: httpx.Client,
        endpoint: OpenHandsSandboxEndpoint,
        source_root: pathlib.Path,
        evidence: TreeEvidence,
        *,
        timeout_seconds: int,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        for item in evidence.files:
            _raise_if_cancelled(
                cancellation_event,
                "OpenHands execution cancelled during source upload",
            )
            policy = WorkspacePathPolicy((item.path,))
            source = ensure_path_policy(
                source_root,
                item.path,
                policy,
                must_exist=True,
            )
            if not source.is_file():
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands source upload encountered a non-file path"
                )
            data = source.read_bytes()
            if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands source upload no longer matches captured tree evidence"
                )

            relative = pathlib.PurePosixPath(item.path)
            remote = pathlib.PurePosixPath(endpoint.working_dir) / relative
            response = client.post(
                "/api/file/upload",
                params={"path": remote.as_posix()},
                files={"file": (relative.name, data, "application/octet-stream")},
                timeout=_bounded_http_timeout(timeout_seconds),
            )
            _require_success_status(response, "source upload")
            _raise_if_cancelled(
                cancellation_event,
                "OpenHands execution cancelled during source upload",
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands source upload returned invalid JSON"
                ) from exc
            if type(payload) is not dict or type(payload.get("success")) is not bool:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands source upload returned non-canonical success evidence"
                )
            if not payload["success"]:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands source upload failed"
                )

    @staticmethod
    def _interrupt_sync(client: httpx.Client, conversation_id: str) -> None:
        response = client.post(
            f"/api/conversations/{conversation_id}/interrupt",
            json={},
            timeout=10.0,
        )
        _require_success_status(response, "conversation interrupt")

    @staticmethod
    def _request_json(
        client: httpx.Client,
        method: str,
        path: str,
        *,
        operation: str,
        json: Any | None = None,
        timeout_seconds: float,
    ) -> Any:
        response = client.request(
            method,
            path,
            json=json,
            timeout=_bounded_http_timeout(timeout_seconds),
        )
        _require_success_status(response, operation)
        try:
            return response.json()
        except ValueError as exc:
            raise OpenHandsAgentServerCompatibilityError(
                f"OpenHands {operation} returned invalid JSON"
            ) from exc


def _bounded_http_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("OpenHands HTTP timeout must be numeric")
    if timeout_seconds <= 0:
        raise TimeoutError
    return min(float(timeout_seconds), _MAX_HTTP_BLOCK_SECONDS)


def _raise_if_cancelled(
    cancellation_event: threading.Event | None,
    message: str,
) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise OpenHandsAgentServerExecutionCancelled(message)


def _canonical_uuid(value: object, *, field: str) -> str:
    if type(value) is not str or value != value.strip():
        raise OpenHandsAgentServerCompatibilityError(
            f"OpenHands {field} must be a canonical UUID string"
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise OpenHandsAgentServerCompatibilityError(
            f"OpenHands {field} must be a canonical UUID string"
        ) from exc
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise OpenHandsAgentServerCompatibilityError(
            f"OpenHands {field} must use canonical UUID spelling"
        )
    return canonical


def _require_success_status(response: httpx.Response, operation: str) -> None:
    if not 200 <= response.status_code < 300:
        raise OpenHandsAgentServerCompatibilityError(
            f"OpenHands {operation} failed with HTTP {response.status_code}"
        )


def _bounded_tag(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_." else "-"
        for character in value
    )
    return (normalized or "nika")[:128]


def _download_snapshot(
    client: httpx.Client,
    endpoint: OpenHandsSandboxEndpoint,
    *,
    baseline_paths: set[str],
    timeout_seconds: int,
    cancellation_event: threading.Event | None = None,
) -> tuple[RemoteFile, ...]:
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b") as spool:
        total = 0
        with client.stream(
            "GET",
            "/api/file/archive",
            params={
                "path": endpoint.working_dir,
                "format": "tar.gz",
                "use_default_excludes": "false",
            },
            timeout=_bounded_http_timeout(timeout_seconds),
        ) as response:
            _require_success_status(response, "snapshot archive")
            for chunk in response.iter_bytes():
                _raise_if_cancelled(
                    cancellation_event,
                    "OpenHands execution cancelled during snapshot download",
                )
                total += len(chunk)
                if total > _MAX_SNAPSHOT_BYTES:
                    raise OpenHandsAgentServerCompatibilityError(
                        "OpenHands snapshot archive exceeds Nika byte limit"
                    )
                spool.write(chunk)
        spool.seek(0)
        return _read_snapshot_archive(
            spool,
            expected_root=pathlib.PurePosixPath(endpoint.working_dir).name,
            baseline_paths=baseline_paths,
            cancellation_event=cancellation_event,
        )


def _read_snapshot_archive(
    fileobj: io.BufferedIOBase,
    *,
    expected_root: str,
    baseline_paths: set[str],
    cancellation_event: threading.Event | None = None,
) -> tuple[RemoteFile, ...]:
    if not expected_root or expected_root in {".", "/"}:
        raise OpenHandsAgentServerCompatibilityError(
            "OpenHands working directory has no stable basename"
        )

    files: dict[str, bytes] = {}
    total_bytes = 0
    member_count = 0
    with tarfile.open(fileobj=fileobj, mode="r:gz") as archive:
        for member in archive:
            _raise_if_cancelled(
                cancellation_event,
                "OpenHands execution cancelled during snapshot validation",
            )
            member_count += 1
            if member_count > _MAX_SNAPSHOT_MEMBERS:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot exceeds Nika member-count limit"
                )
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot contains an unsafe path"
                )
            if path.parts[0] != expected_root:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot root identity changed"
                )
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot contains a non-regular filesystem entry"
                )
            if len(path.parts) < 2:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot contains an invalid member"
                )
            relative = pathlib.PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot contains duplicate files"
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot member is unreadable"
                )
            data = extracted.read(_MAX_SNAPSHOT_BYTES + 1)
            if len(data) != member.size:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot member size changed"
                )
            total_bytes += len(data)
            if total_bytes > _MAX_SNAPSHOT_BYTES:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot exceeds Nika byte limit"
                )
            files[relative] = data
            if len(files) > _MAX_SNAPSHOT_FILES + 1:
                raise OpenHandsAgentServerCompatibilityError(
                    "OpenHands snapshot exceeds Nika file limit"
                )

    synthetic = files.get("archive_manifest.json")
    if "archive_manifest.json" not in baseline_paths and synthetic is not None:
        ordinary = {
            path: data
            for path, data in files.items()
            if path != "archive_manifest.json"
        }
        if _is_synthetic_archive_manifest(synthetic, expected_root, ordinary):
            del files["archive_manifest.json"]

    if len(files) > _MAX_SNAPSHOT_FILES:
        raise OpenHandsAgentServerCompatibilityError(
            "OpenHands snapshot exceeds Nika file limit"
        )
    return tuple(
        RemoteFile(path, files[path])
        for path in sorted(files, key=str.casefold)
    )


def _is_synthetic_archive_manifest(
    data: bytes,
    expected_root: str,
    ordinary_files: dict[str, bytes],
) -> bool:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("format") == "tar.gz"
        and payload.get("source") == expected_root
        and payload.get("file_count") == len(ordinary_files)
        and payload.get("total_bytes") == sum(
            len(value) for value in ordinary_files.values()
        )
        and payload.get("excludes") == []
    )


# Compatibility aliases for callers already wired to the #843 module path. These
# names no longer import or instantiate the OpenHands Python SDK.
OpenHandsSdkCompatibilityError = OpenHandsAgentServerCompatibilityError
OpenHandsSdkExecutionCancelled = OpenHandsAgentServerExecutionCancelled
OpenHandsSdkRemoteRuntime = OpenHandsAgentServerRuntime
