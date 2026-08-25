from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

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


class _BlockingAuthority:
    def __init__(self) -> None:
        self.block = False
        self.started = Event()
        self.release = Event()

    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None:
        del approval_refs
        assert subject.kind == "workspace"
        if self.block:
            self.started.set()
            assert self.release.wait(timeout=5)


def _plugin(version: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="aud03.workspace.plugin",
        name="AUD03 workspace plugin",
        version=version,
        entrypoint_name="aud03-workspace-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="aud03.workspace.capability",
                description="AUD03 workspace capability",
            ),
        ),
        permission_ids=("workspace.read",),
        action_ids=("workspace.open",),
    )


def _workspace(version: str) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="aud03.workspace.activation",
        name="AUD03 workspace activation",
        version=version,
        required_plugins=(
            PluginRequirement(
                plugin_id="aud03.workspace.plugin",
                required_capabilities=("aud03.workspace.capability",),
                required_permission_ids=("workspace.read",),
                required_action_ids=("workspace.open",),
            ),
        ),
    )


def _repository(
    path: Path,
    plugins: dict[str, PluginManifest],
    authority: _BlockingAuthority,
) -> WorkspaceActivationRepository:
    return WorkspaceActivationRepository(
        SQLiteStore(path),
        WorkspaceCatalog(
            WorkspacePolicyCatalog(
                permission_ids=frozenset({"workspace.read"}),
                action_ids=frozenset({"workspace.open"}),
            )
        ),
        lambda: dict(plugins),
        activation_authority=authority,
    )


def test_plugin_drift_during_authority_check_cannot_replace_prior_active_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()

    plugin_v1 = _plugin("1.0.0")
    plugins = {plugin_v1.plugin_id: plugin_v1}
    authority = _BlockingAuthority()
    repository = _repository(path, plugins, authority)

    generation_one = repository.save_candidate(_workspace("1.0.0"))
    active_one = repository.activate(
        generation_one.workspace.workspace_id,
        generation_one.generation,
    )
    assert active_one.generation == generation_one.generation

    generation_two = repository.save_candidate(_workspace("2.0.0"))
    authority.block = True
    failures: list[Exception] = []

    def activate_generation_two() -> None:
        try:
            repository.activate(
                generation_two.workspace.workspace_id,
                generation_two.generation,
            )
        except Exception as exc:  # noqa: BLE001 - oracle records the failed activation.
            failures.append(exc)

    thread = Thread(target=activate_generation_two)
    thread.start()
    assert authority.started.wait(timeout=5)

    plugin_v2 = _plugin("2.0.0")
    plugins[plugin_v2.plugin_id] = plugin_v2
    authority.release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures

    stored_one = repository.get(
        generation_one.workspace.workspace_id,
        generation_one.generation,
    )
    stored_two = repository.get(
        generation_two.workspace.workspace_id,
        generation_two.generation,
    )
    assert stored_one is not None
    assert stored_two is not None
    assert stored_one.status == "active"
    assert stored_two.status == "candidate"


def test_workspace_activation_still_replaces_prior_generation_without_plugin_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()

    plugin = _plugin("1.0.0")
    plugins = {plugin.plugin_id: plugin}
    authority = _BlockingAuthority()
    repository = _repository(path, plugins, authority)

    generation_one = repository.save_candidate(_workspace("1.0.0"))
    repository.activate(
        generation_one.workspace.workspace_id,
        generation_one.generation,
    )
    generation_two = repository.save_candidate(_workspace("2.0.0"))
    active_two = repository.activate(
        generation_two.workspace.workspace_id,
        generation_two.generation,
    )

    stored_one = repository.get(
        generation_one.workspace.workspace_id,
        generation_one.generation,
    )
    assert stored_one is not None
    assert stored_one.status == "retired"
    assert active_two.generation == generation_two.generation
    assert active_two.status == "active"
