from __future__ import annotations

import pytest

from nika_core.plugins import CURRENT_PLUGIN_API, PluginManifest
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceCatalog,
    WorkspaceCompatibilityError,
    WorkspaceManifest,
)


def _workspace(api_min: int, api_max: int) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="test.workspace",
        name="Workspace",
        version="1",
        required_plugins=(
            PluginRequirement(
                plugin_id="test.plugin",
                api_min=api_min,
                api_max=api_max,
            ),
        ),
    )


def _plugin(api_min: int, api_max: int) -> PluginManifest:
    return PluginManifest(
        plugin_id="test.plugin",
        name="Plugin",
        version="1",
        entrypoint_name="test-plugin",
        plugin_api_min=api_min,
        plugin_api_max=api_max,
    )


def test_workspace_requires_actual_core_api_even_if_plugin_supports_future_api() -> None:
    current = CURRENT_PLUGIN_API
    plugin = _plugin(current, current + 1)
    workspace = _workspace(current + 1, current + 1)

    with pytest.raises(WorkspaceCompatibilityError, match="incompatible plugin API"):
        WorkspaceCatalog().validate(workspace, {plugin.plugin_id: plugin})


def test_workspace_accepts_shared_actual_api_without_requiring_newer_supported_api() -> None:
    current = CURRENT_PLUGIN_API
    plugin = _plugin(current, current + 1)
    workspace = _workspace(current, current + 1)

    WorkspaceCatalog().validate(workspace, {plugin.plugin_id: plugin})


def test_workspace_does_not_activate_plugin_unsupported_by_actual_core_api() -> None:
    current = CURRENT_PLUGIN_API
    plugin = _plugin(current + 1, current + 2)
    workspace = _workspace(current, current + 2)

    with pytest.raises(WorkspaceCompatibilityError, match="supports API"):
        WorkspaceCatalog().validate(workspace, {plugin.plugin_id: plugin})
