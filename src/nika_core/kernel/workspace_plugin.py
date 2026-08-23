"""Compatibility import path for the provider-neutral workspace SDK contracts."""

from nika_core.workspace_sdk import (
    WORKSPACE_ENTRYPOINT_GROUP,
    WorkspaceEntrypointDescriptor,
    WorkspacePlugin,
    discover_workspace_entrypoints,
)

__all__ = [
    "WORKSPACE_ENTRYPOINT_GROUP",
    "WorkspaceEntrypointDescriptor",
    "WorkspacePlugin",
    "discover_workspace_entrypoints",
]
