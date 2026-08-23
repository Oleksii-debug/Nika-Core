from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.workspace_sdk import (
    WorkspaceActivationService,
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
    WorkspacePluginRepository,
    WorkspaceValidationCatalog,
    compile_workspace_activation,
)


def _manifest(version: int, permission: str) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="aud03.workspace",
        version=version,
        display_name=f"AUD03 workspace v{version}",
        permission_ids=(permission,),
    )


def _descriptor() -> WorkspaceEntrypointDescriptor:
    return WorkspaceEntrypointDescriptor(name="aud03", value="aud03_plugin:plugin")


def _catalog() -> WorkspaceValidationCatalog:
    return WorkspaceValidationCatalog(
        permission_ids=frozenset({"workspace.read", "workspace.write"})
    )


def _proposal(manifest: WorkspaceManifest):
    return compile_workspace_activation(
        manifest,
        _descriptor(),
        catalog=_catalog(),
        approved_permission_ids=frozenset(manifest.permission_ids),
    )


def test_stale_candidate_cannot_replace_newer_active_workspace_after_restart(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = WorkspacePluginRepository(store)
    first = _manifest(1, "workspace.read")
    second = _manifest(2, "workspace.write")
    repository.save_candidate(_proposal(first))
    repository.save_candidate(_proposal(second))

    service = WorkspaceActivationService(repository, _catalog())
    service.activate(
        second,
        _descriptor(),
        approved_permission_ids=frozenset({"workspace.write"}),
    )
    assert repository.active(first.workspace_id).manifest.version == 2

    restarted = WorkspacePluginRepository(SQLiteStore(store.path))
    stale_service = WorkspaceActivationService(restarted, _catalog())
    with pytest.raises((ValueError, RuntimeError)):
        stale_service.activate(
            first,
            _descriptor(),
            approved_permission_ids=frozenset({"workspace.read"}),
        )

    active = restarted.active(first.workspace_id)
    assert active is not None
    assert active.manifest.version == 2
    assert active.effective_permission_ids == ("workspace.write",)
