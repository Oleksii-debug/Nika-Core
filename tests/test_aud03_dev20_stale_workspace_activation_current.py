from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.plugins import CapabilityDeclaration, PluginManifest
from nika_core.tools import ToolRisk
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceActivationRepository,
    WorkspaceCatalog,
    WorkspaceManifest,
    WorkspacePolicyCatalog,
)
from nika_core.workspaces.catalog import WorkspaceCapabilityGrant


def _plugin() -> PluginManifest:
    return PluginManifest(
        plugin_id="aud03.plugin",
        name="AUD03 plugin",
        version="1.0.0",
        entrypoint_name="aud03-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="aud03.read",
                risk=ToolRisk.READ_ONLY,
                description="Read-only AUD03 fixture",
            ),
        ),
        permission_ids=("workspace.read",),
        action_ids=("workspace.open",),
    )


def _workspace(version: str) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="aud03.workspace",
        name="AUD03 workspace",
        version=version,
        required_plugins=(
            PluginRequirement(
                plugin_id="aud03.plugin",
                required_capabilities=("aud03.read",),
                required_permission_ids=("workspace.read",),
                required_action_ids=("workspace.open",),
            ),
        ),
        capability_grants=(
            WorkspaceCapabilityGrant(
                plugin_id="aud03.plugin",
                capability_id="aud03.read",
            ),
        ),
    )


def _catalog() -> WorkspaceCatalog:
    return WorkspaceCatalog(
        WorkspacePolicyCatalog(
            permission_ids=frozenset({"workspace.read"}),
            action_ids=frozenset({"workspace.open"}),
        )
    )


def test_restart_rejects_stale_generation_after_newer_workspace_is_active(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    plugin = _plugin()
    plugins = lambda: {plugin.plugin_id: plugin}

    repository = WorkspaceActivationRepository(store, _catalog(), plugins)
    first = repository.save_candidate(_workspace("1.0.0"))
    second = repository.save_candidate(_workspace("2.0.0"))
    repository.activate(second.workspace.workspace_id, second.generation)

    restarted = WorkspaceActivationRepository(SQLiteStore(path), _catalog(), plugins)
    with pytest.raises(PermissionError, match="stale workspace activation"):
        restarted.activate(first.workspace.workspace_id, first.generation)

    active = restarted.active(first.workspace.workspace_id)
    assert active is not None
    assert active.generation == second.generation
    assert active.workspace.version == "2.0.0"
