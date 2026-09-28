from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.activation_authority import ActivationSubject
from nika_core.data.sqlite import SQLiteStore
from nika_core.plugins import CapabilityDeclaration, PluginManifest
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceActivationRepository,
    WorkspaceCatalog,
    WorkspaceManifest,
    WorkspacePolicyCatalog,
)


class _TrustedAuthority:
    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None:
        assert subject.kind == "workspace"
        if approval_refs != ("approval://aud03-host",):
            raise PermissionError("untrusted activation authority")


def _plugin() -> PluginManifest:
    return PluginManifest(
        plugin_id="aud03.plugin",
        name="AUD03 plugin",
        version="1.0.0",
        entrypoint_name="aud03-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="aud03.capability",
                description="AUD03 deterministic capability",
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
                required_capabilities=("aud03.capability",),
                required_permission_ids=("workspace.read",),
                required_action_ids=("workspace.open",),
            ),
        ),
    )


def _repository(path: Path) -> WorkspaceActivationRepository:
    plugin = _plugin()
    return WorkspaceActivationRepository(
        SQLiteStore(path),
        WorkspaceCatalog(
            WorkspacePolicyCatalog(
                permission_ids=frozenset({"workspace.read"}),
                action_ids=frozenset({"workspace.open"}),
            )
        ),
        lambda: {plugin.plugin_id: plugin},
        activation_authority=_TrustedAuthority(),
    )


def test_restart_cannot_roll_back_newer_workspace_activation(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    repository = _repository(path)

    generation_one = repository.save_candidate(_workspace("1.0.0"))
    generation_two = repository.save_candidate(_workspace("2.0.0"))
    activated = repository.activate(
        generation_two.workspace.workspace_id,
        generation_two.generation,
        approval_refs=("approval://aud03-host",),
    )
    assert activated.generation == generation_two.generation

    restarted = _repository(path)
    with pytest.raises(PermissionError, match="stale workspace activation"):
        restarted.activate(
            generation_one.workspace.workspace_id,
            generation_one.generation,
            approval_refs=("approval://aud03-host",),
        )

    current = restarted.active(generation_one.workspace.workspace_id)
    assert current is not None
    assert current.generation == generation_two.generation
    assert current.workspace.version == "2.0.0"
    assert current.status == "active"
