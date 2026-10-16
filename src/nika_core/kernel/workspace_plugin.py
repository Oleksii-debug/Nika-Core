from __future__ import annotations

from typing import Protocol, runtime_checkable

from nika_core.plugins.entrypoints import EntrypointDescriptor, discover_entrypoints

WORKSPACE_ENTRYPOINT_GROUP = "nika_core.workspaces"


@runtime_checkable
class WorkspacePlugin(Protocol):
    """Stable minimum contract for independently packaged Nika workspaces."""

    workspace_id: str
    version: int

    def display_name(self) -> str: ...


def discover_workspace_entrypoints() -> tuple[EntrypointDescriptor, ...]:
    """Discover installed workspace package metadata without importing plugin code."""
    return discover_entrypoints(WORKSPACE_ENTRYPOINT_GROUP)
