from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, runtime_checkable

from nika_core.tools import ToolRisk

from .catalog import PluginRequirement, WorkspaceCapabilityGrant, WorkspaceManifest


SOFTWARE_FACTORY_MANIFEST = WorkspaceManifest(
    workspace_id="software.factory",
    name="Software Factory",
    version="1.0.0",
    required_plugins=(
        PluginRequirement(
            plugin_id="coding.worker",
            required_capabilities=(
                "coding.repository.read",
                "coding.workspace.write",
                "coding.tests.run",
            ),
        ),
    ),
    capability_grants=(
        WorkspaceCapabilityGrant(
            plugin_id="coding.worker",
            capability_id="coding.repository.read",
            max_risk=ToolRisk.READ_ONLY,
        ),
        WorkspaceCapabilityGrant(
            plugin_id="coding.worker",
            capability_id="coding.workspace.write",
            max_risk=ToolRisk.LOCAL_WRITE,
        ),
        WorkspaceCapabilityGrant(
            plugin_id="coding.worker",
            capability_id="coding.tests.run",
            max_risk=ToolRisk.LOCAL_WRITE,
        ),
    ),
    data_roots=("artifacts", "worktrees"),
)


def _normalize_relative_path(value: str) -> PurePosixPath:
    stripped = value.strip()
    windows_path = PureWindowsPath(stripped)
    normalized = PurePosixPath(stripped.replace("\\", "/"))
    if (
        not stripped
        or normalized == PurePosixPath(".")
        or windows_path.drive
        or normalized.is_absolute()
        or ".." in normalized.parts
    ):
        raise ValueError("path must stay inside a bounded repository-relative scope")
    return normalized


@dataclass(frozen=True, slots=True)
class CodingRequest:
    repository_root: Path
    goal: str
    allowed_paths: tuple[str, ...]
    test_commands: tuple[str, ...] = ()
    network_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.allowed_paths:
            raise ValueError("at least one allowed path is required")
        for path in self.allowed_paths:
            _normalize_relative_path(path)


@dataclass(frozen=True, slots=True)
class CodingResult:
    changed_paths: tuple[str, ...]
    test_evidence: tuple[str, ...]
    patch_ref: str | None = None
    commit_sha: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    """Evidence that a task lacks a safe existing capability and needs engineering work."""

    task_id: str
    capability_id: str
    evidence: str
    attempted_methods: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.capability_id.strip() or not self.evidence.strip():
            raise ValueError("capability gap fields must not be empty")
        if not self.attempted_methods:
            raise ValueError("capability gap must record attempted methods")


@runtime_checkable
class CodingWorkerPort(Protocol):
    """Framework-neutral boundary for OpenHands or another isolated coding worker."""

    async def execute(self, request: CodingRequest) -> CodingResult: ...

    async def cancel(self, task_id: str) -> None: ...


class SoftwareFactoryService:
    """Validate coding-worker evidence before it can leave an isolated workspace boundary."""

    def __init__(self, worker: CodingWorkerPort) -> None:
        self._worker = worker

    async def execute(self, request: CodingRequest) -> CodingResult:
        result = await self._worker.execute(request)
        self._validate_result(request, result)
        return result

    async def cancel(self, task_id: str) -> None:
        await self._worker.cancel(task_id)

    @staticmethod
    def _validate_result(request: CodingRequest, result: CodingResult) -> None:
        allowed = tuple(_normalize_relative_path(path) for path in request.allowed_paths)
        for changed_path in result.changed_paths:
            candidate = _normalize_relative_path(changed_path)
            if not any(candidate == root or root in candidate.parents for root in allowed):
                raise ValueError(f"coding worker changed path outside allowed scope: {changed_path}")
        if request.test_commands and not result.test_evidence:
            raise ValueError("coding worker returned no test evidence for required verification")
        if result.changed_paths and result.patch_ref is None and result.commit_sha is None:
            raise ValueError("coding worker changes require patch_ref or commit_sha evidence")
