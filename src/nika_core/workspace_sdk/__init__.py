"""Versioned, permission-bounded workspace plugin SDK contracts."""

from nika_core.workspace_sdk.activation import WorkspaceActivationService
from nika_core.workspace_sdk.contracts import (
    WorkspaceActivationProposal,
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
    WorkspacePlugin,
    WorkspaceValidationCatalog,
    compile_workspace_activation,
)
from nika_core.workspace_sdk.discovery import (
    WORKSPACE_ENTRYPOINT_GROUP,
    LoadedWorkspacePlugin,
    WorkspaceDiscoveryReport,
    WorkspaceLoadFailure,
    discover_workspace_entrypoints,
    load_workspace_plugins,
)
from nika_core.workspace_sdk.repository import StoredWorkspacePlugin, WorkspacePluginRepository

__all__ = [
    "WORKSPACE_ENTRYPOINT_GROUP",
    "LoadedWorkspacePlugin",
    "StoredWorkspacePlugin",
    "WorkspaceActivationProposal",
    "WorkspaceActivationService",
    "WorkspaceDiscoveryReport",
    "WorkspaceEntrypointDescriptor",
    "WorkspaceLoadFailure",
    "WorkspaceManifest",
    "WorkspacePlugin",
    "WorkspacePluginRepository",
    "WorkspaceValidationCatalog",
    "compile_workspace_activation",
    "discover_workspace_entrypoints",
    "load_workspace_plugins",
]
