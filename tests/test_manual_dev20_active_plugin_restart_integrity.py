from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.plugins import PluginManifest
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceActivationRepository,
    WorkspaceCatalog,
    WorkspaceCompatibilityError,
    WorkspaceManifest,
)


def _plugin(version: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="restart.plugin",
        name="Restart plugin",
        version=version,
        entrypoint_name="restart-plugin",
    )


def _workspace() -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="restart.workspace",
        name="Restart workspace",
        version="1.0.0",
        required_plugins=(PluginRequirement(plugin_id="restart.plugin"),),
    )


def test_active_workspace_restart_rejects_installed_plugin_drift(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    state = {"manifest": _plugin("1.0.0")}

    def plugins() -> dict[str, PluginManifest]:
        return {"restart.plugin": state["manifest"]}

    repository = WorkspaceActivationRepository(store, WorkspaceCatalog(), plugins)
    candidate = repository.save_candidate(_workspace())
    repository.activate(candidate.workspace.workspace_id, candidate.generation)

    state["manifest"] = _plugin("2.0.0")
    restarted = WorkspaceActivationRepository(
        SQLiteStore(store.path),
        WorkspaceCatalog(),
        plugins,
    )
    with pytest.raises(PermissionError, match="plugin manifest changed"):
        restarted.active(candidate.workspace.workspace_id)


def test_active_workspace_restart_rejects_missing_plugin(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifest = _plugin("1.0.0")
    repository = WorkspaceActivationRepository(
        store,
        WorkspaceCatalog(),
        lambda: {"restart.plugin": manifest},
    )
    candidate = repository.save_candidate(_workspace())
    repository.activate(candidate.workspace.workspace_id, candidate.generation)

    restarted = WorkspaceActivationRepository(
        SQLiteStore(store.path),
        WorkspaceCatalog(),
        lambda: {},
    )
    with pytest.raises(WorkspaceCompatibilityError, match="missing required plugin"):
        restarted.active(candidate.workspace.workspace_id)
