from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.metadata
import io
import json
import logging
import pathlib
import tarfile
import tempfile
import threading
from collections.abc import Callable
from typing import Any

from nika_core.toolsmith.contracts import CodingJob
from nika_core.toolsmith.openhands_remote_worker import (
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

_OPENHANDS_SDK_VERSION = "1.49.2"
_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 2000


class OpenHandsSdkCompatibilityError(RuntimeError):
    """Raised when the installed OpenHands SDK cannot satisfy the pinned adapter contract."""


class OpenHandsSdkExecutionCancelled(RuntimeError):
    """Raised inside the SDK thread when Nika requested cancellation before/during a run."""


@dataclasses.dataclass(slots=True)
class _ActiveExecution:
    conversation: Any | None = None
    cancel_requested: threading.Event = dataclasses.field(default_factory=threading.Event)
    done: threading.Event = dataclasses.field(default_factory=threading.Event)


class OpenHandsSdkRemoteRuntime:
    """Thin OpenHands SDK adapter for an already-provisioned remote sandbox.

    The workspace factory owns endpoint authentication and credential redemption.
    The agent factory owns Nika's model-route-to-OpenHands-Agent projection.
    This class deliberately owns neither credential storage nor model routing.
    """

    def __init__(
        self,
        *,
        workspace_factory: Callable[[OpenHandsSandboxEndpoint], Any],
        agent_factory: Callable[[CodingJob, OpenHandsSandboxEndpoint], Any],
        max_iterations: int = 96,
    ) -> None:
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
        ):
            raise ValueError("OpenHands max_iterations must be a positive integer")
        self._workspace_factory = workspace_factory
        self._agent_factory = agent_factory
        self._max_iterations = max_iterations
        self._active: dict[str, _ActiveExecution] = {}
        self._cancelled_done: set[str] = set()
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
                raise OpenHandsSdkCompatibilityError(
                    "OpenHands SDK job identity is already active"
                )
            if job.job_id in self._pending_cancel:
                self._pending_cancel.remove(job.job_id)
                active.cancel_requested.set()
            self._cancelled_done.discard(job.job_id)
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
            raise

    async def cancel(self, job_id: str) -> bool:
        with self._active_lock:
            active = self._active.get(job_id)
            if active is None:
                if job_id in self._cancelled_done:
                    self._cancelled_done.remove(job_id)
                    # Retain the consumed terminal proof as a pending reservation. A
                    # repeated cancel is then idempotently false, while any forbidden
                    # direct reuse of the same runtime job identity still fails before
                    # workspace acquisition.
                    self._pending_cancel.add(job_id)
                    return True
                if job_id in self._pending_cancel:
                    return False
                # A worker may reserve remote dispatch and be cancelled in the tiny
                # scheduling window before execute() registers _active. Returning a
                # proven reservation means a later execute for this job must fail
                # before any external workspace or conversation effect.
                self._pending_cancel.add(job_id)
                return True
            active.cancel_requested.set()
            conversation = active.conversation

        if conversation is not None:
            try:
                await asyncio.to_thread(conversation.interrupt)
            except Exception as exc:  # noqa: BLE001 - third-party SDK boundary
                _LOGGER.warning(
                    "OpenHands interrupt failed; remote stop remains unverified (%s)",
                    type(exc).__name__,
                )

        # Cancellation can arrive while source upload/agent creation is still running,
        # before a Conversation exists. The sync thread observes cancel_requested before
        # send/run. A stop is proven only when that thread reaches its finalizer.
        for _ in range(50):
            if active.done.is_set():
                return True
            await asyncio.sleep(0.1)
        return active.done.is_set()

    def _execute_sync(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        prompt: str,
        source_root: pathlib.Path,
        source_evidence: TreeEvidence,
        active: _ActiveExecution,
    ) -> OpenHandsRunEvidence:
        conversation = None
        try:
            if active.cancel_requested.is_set():
                raise OpenHandsSdkExecutionCancelled(
                    "OpenHands execution cancelled before workspace acquisition"
                )
            Conversation = _load_conversation_type()
            workspace = self._workspace_factory(endpoint)
            if getattr(workspace, "working_dir", None) != endpoint.working_dir:
                raise OpenHandsSdkCompatibilityError(
                    "workspace factory returned a different remote working directory"
                )
            if getattr(workspace, "host", "").rstrip("/") != endpoint.host.rstrip("/"):
                raise OpenHandsSdkCompatibilityError(
                    "workspace factory returned a different endpoint"
                )

            with workspace:
                self._upload_source(workspace, endpoint, source_root, source_evidence)
                if active.cancel_requested.is_set():
                    raise OpenHandsSdkExecutionCancelled(
                        "OpenHands execution cancelled before agent creation"
                    )

                agent = self._agent_factory(job, endpoint)
                if active.cancel_requested.is_set():
                    raise OpenHandsSdkExecutionCancelled(
                        "OpenHands execution cancelled before conversation creation"
                    )

                conversation = Conversation(
                    agent=agent,
                    workspace=workspace,
                    max_iteration_per_run=self._max_iterations,
                    visualizer=None,
                    delete_on_close=True,
                    tags={
                        "nika_job": _bounded_tag(job.job_id),
                        "nika_task": _bounded_tag(job.task_id),
                    },
                )
                with self._active_lock:
                    active.conversation = conversation
                if active.cancel_requested.is_set():
                    raise OpenHandsSdkExecutionCancelled(
                        "OpenHands execution cancelled before message dispatch"
                    )

                conversation.send_message(prompt, sender="nika-core")
                if active.cancel_requested.is_set():
                    raise OpenHandsSdkExecutionCancelled(
                        "OpenHands execution cancelled before conversation run"
                    )
                conversation.run(
                    blocking=True,
                    timeout=float(job.resource_budget.timeout_seconds),
                )
                if active.cancel_requested.is_set():
                    raise OpenHandsSdkExecutionCancelled(
                        "OpenHands execution cancelled before snapshot collection"
                    )
                files = _download_snapshot(
                    workspace,
                    endpoint,
                    baseline_paths={item.path for item in source_evidence.files},
                    timeout_seconds=job.resource_budget.timeout_seconds,
                )
                return OpenHandsRunEvidence(str(conversation.id), files)
        finally:
            if conversation is not None:
                try:
                    conversation.close()
                except Exception as exc:  # noqa: BLE001 - third-party SDK boundary
                    _LOGGER.warning(
                        "OpenHands conversation close requires reconciliation (%s)",
                        type(exc).__name__,
                    )
            active.done.set()
            with self._active_lock:
                if active.cancel_requested.is_set():
                    self._cancelled_done.add(job.job_id)
                if self._active.get(job.job_id) is active:
                    self._active.pop(job.job_id, None)

    @staticmethod
    def _upload_source(
        workspace: Any,
        endpoint: OpenHandsSandboxEndpoint,
        source_root: pathlib.Path,
        evidence: TreeEvidence,
    ) -> None:
        # The source tree may change after endpoint acquisition. Freeze each uploaded
        # byte string only after revalidating it against the captured TreeEvidence.
        with tempfile.TemporaryDirectory(prefix="nika-openhands-source-") as frozen_root_text:
            frozen_root = pathlib.Path(frozen_root_text)
            for item in evidence.files:
                policy = WorkspacePathPolicy((item.path,))
                source = ensure_path_policy(
                    source_root,
                    item.path,
                    policy,
                    must_exist=True,
                )
                if not source.is_file():
                    raise OpenHandsSdkCompatibilityError(
                        "OpenHands source upload encountered a non-file path"
                    )
                data = source.read_bytes()
                if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                    raise OpenHandsSdkCompatibilityError(
                        "OpenHands source upload no longer matches captured tree evidence"
                    )

                relative = pathlib.PurePosixPath(item.path)
                frozen = frozen_root.joinpath(*relative.parts)
                frozen.parent.mkdir(parents=True, exist_ok=True)
                frozen.write_bytes(data)
                remote = pathlib.PurePosixPath(endpoint.working_dir) / relative
                result = workspace.file_upload(frozen, remote.as_posix())
                if not getattr(result, "success", False):
                    raise OpenHandsSdkCompatibilityError("OpenHands source upload failed")


def _load_conversation_type() -> type[Any]:
    try:
        installed = importlib.metadata.version("openhands-sdk")
    except importlib.metadata.PackageNotFoundError as exc:
        raise OpenHandsSdkCompatibilityError(
            "OpenHands coding backend requires the 'coding-openhands' optional dependency"
        ) from exc
    if installed != _OPENHANDS_SDK_VERSION:
        raise OpenHandsSdkCompatibilityError(
            f"OpenHands SDK version mismatch: expected {_OPENHANDS_SDK_VERSION}, got {installed}"
        )
    try:
        from openhands.sdk import Conversation
    except Exception as exc:
        raise OpenHandsSdkCompatibilityError("OpenHands SDK import failed") from exc
    return Conversation


def _bounded_tag(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_." else "-"
        for character in value
    )
    return (normalized or "nika")[:128]


def _download_snapshot(
    workspace: Any,
    endpoint: OpenHandsSandboxEndpoint,
    *,
    baseline_paths: set[str],
    timeout_seconds: int,
) -> tuple[RemoteFile, ...]:
    api_prefix = getattr(workspace, "api_prefix", None)
    client = getattr(workspace, "client", None)
    if not isinstance(api_prefix, str) or client is None:
        raise OpenHandsSdkCompatibilityError("OpenHands workspace lacks remote archive capability")
    route = f"{api_prefix.rstrip('/')}/file/archive"
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b") as spool:
        total = 0
        with client.stream(
            "GET",
            route,
            params={
                "path": endpoint.working_dir,
                "format": "tar.gz",
                "use_default_excludes": "false",
            },
            timeout=float(timeout_seconds),
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_SNAPSHOT_BYTES:
                    raise OpenHandsSdkCompatibilityError(
                        "OpenHands snapshot archive exceeds Nika byte limit"
                    )
                spool.write(chunk)
        spool.seek(0)
        return _read_snapshot_archive(
            spool,
            expected_root=pathlib.PurePosixPath(endpoint.working_dir).name,
            baseline_paths=baseline_paths,
        )


def _read_snapshot_archive(
    fileobj: io.BufferedIOBase,
    *,
    expected_root: str,
    baseline_paths: set[str],
) -> tuple[RemoteFile, ...]:
    if not expected_root or expected_root in {".", "/"}:
        raise OpenHandsSdkCompatibilityError("OpenHands working directory has no stable basename")

    files: dict[str, bytes] = {}
    total_bytes = 0
    with tarfile.open(fileobj=fileobj, mode="r:gz") as archive:
        for member in archive:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot contains an unsafe path")
            if path.parts[0] != expected_root:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot root identity changed")
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise OpenHandsSdkCompatibilityError(
                    "OpenHands snapshot contains a non-regular filesystem entry"
                )
            if len(path.parts) < 2:
                raise OpenHandsSdkCompatibilityError(
                    "OpenHands snapshot contains an invalid member"
                )
            relative = pathlib.PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot contains duplicate files")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot member is unreadable")
            data = extracted.read(_MAX_SNAPSHOT_BYTES + 1)
            if len(data) != member.size:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot member size changed")
            total_bytes += len(data)
            if total_bytes > _MAX_SNAPSHOT_BYTES:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot exceeds Nika byte limit")
            files[relative] = data
            if len(files) > _MAX_SNAPSHOT_FILES + 1:
                raise OpenHandsSdkCompatibilityError("OpenHands snapshot exceeds Nika file limit")

    synthetic = files.get("archive_manifest.json")
    if "archive_manifest.json" not in baseline_paths and synthetic is not None:
        ordinary = {path: data for path, data in files.items() if path != "archive_manifest.json"}
        if _is_synthetic_archive_manifest(synthetic, expected_root, ordinary):
            del files["archive_manifest.json"]

    if len(files) > _MAX_SNAPSHOT_FILES:
        raise OpenHandsSdkCompatibilityError("OpenHands snapshot exceeds Nika file limit")
    return tuple(RemoteFile(path, files[path]) for path in sorted(files, key=str.casefold))


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
        and payload.get("total_bytes") == sum(len(value) for value in ordinary_files.values())
        and payload.get("excludes") == []
    )
