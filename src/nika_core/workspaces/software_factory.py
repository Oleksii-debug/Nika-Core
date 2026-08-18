from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
        if any(Path(path).is_absolute() for path in self.allowed_paths):
            raise ValueError("allowed_paths must be repository-relative")
        if any(".." in Path(path).parts for path in self.allowed_paths):
            raise ValueError("allowed_paths must not traverse outside the repository")


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
