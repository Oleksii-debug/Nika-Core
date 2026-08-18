from __future__ import annotations

import importlib.metadata
from typing import Protocol, runtime_checkable

WORKSPACE_ENTRYPOINT_GROUP = "nika_core.workspaces"


@runtime_checkable
class WorkspacePlugin(Protocol):
    """Stable minimum contract for independently packaged Nika workspaces."""

    workspace_id: str
    version: int

    def display_name(self) -> str: ...


def discover_workspace_entrypoints() -> tuple[importlib.metadata.EntryPoint, ...]:
    """Discover installed workspace packages without importing them eagerly."""
    return tuple(importlib.metadata.entry_points(group=WORKSPACE_ENTRYPOINT_GROUP))
