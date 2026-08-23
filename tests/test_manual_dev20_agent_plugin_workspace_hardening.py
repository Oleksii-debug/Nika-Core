from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.activation_authority import ActivationSubject
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.workspace_plugin import discover_workspace_entrypoints
from nika_core.plugins import (
    CapabilityDeclaration,
    EntrypointDescriptor,
    PluginCompatibilityError,
    PluginManifest,
    PluginPolicyCatalog,
    PluginRegistration,
    PluginRuntime,
    discover_plugin_entrypoints,
    inspect_plugin_entrypoints,
)
from nika_core.plugins import entrypoints
from nika_core.tools import ToolRisk
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceActivationRepository,
    WorkspaceCatalog,
    WorkspaceManifest,
    WorkspacePolicyCatalog,
)
from nika_core.workspaces.catalog import WorkspaceCapabilityGrant


class _Authority:
    def __init__(self) -> None:
        self.calls: list[tuple[ActivationSubject, tuple[str, ...]]] = []

    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None:
        self.calls.append((subject, approval_refs))
        if approval_refs != ("approval://trusted",):
            raise PermissionError("untrusted approval evidence")


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _plugin(
    *,
    version: str = "1.0.0",
    permissions: tuple[str, ...] = ("workspace.read", "workspace.write"),
    actions: tuple[str, ...] = ("workspace.open",),
    risk: ToolRisk = ToolRisk.READ_ONLY,
) -> PluginManifest:
    return PluginManifest(
        plugin_id="research.plugin",
        name="Research plugin",
        version=version,
        entrypoint_name="research-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="research.capture",
                risk=risk,
                description="Capture research evidence",
            ),
        ),
        permission_ids=permissions,
        action_ids=actions,
    )


def _workspace(*, version: str) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="research.workspace",
        name="Research workspace",
        version=version,
        required_plugins=(
            PluginRequirement(
                plugin_id="research.plugin",
                required_capabilities=("research.capture",),
                required_permission_ids=("workspace.read",),
                required_action_ids=("workspace.open",),
            ),
        ),
        capability_grants=(
            WorkspaceCapabilityGrant(
                plugin_id="research.plugin",
                capability_id="research.capture",
            ),
        ),
    )


def _workspace_catalog() -> WorkspaceCatalog:
    return WorkspaceCatalog(
        WorkspacePolicyCatalog(
            permission_ids=frozenset({"workspace.read", "workspace.write"}),
            action_ids=frozenset({"workspace.open"}),
        )
    )


def test_plugin_manifest_ids_fail_closed_and_activation_permissions_do_not_expand() -> None:
    catalog = PluginPolicyCatalog(
        permission_ids=frozenset({"workspace.read", "workspace.write"}),
        action_ids=frozenset({"workspace.open"}),
    )
    with pytest.raises(PluginCompatibilityError, match="unknown plugin permission"):
        PluginRuntime(policy_catalog=catalog).register(
            _plugin(permissions=("workspace.admin",)),
            lambda: _Adapter(_plugin(permissions=("workspace.admin",))),
        )

    manifest = _plugin(permissions=("workspace.read",))
    runtime = PluginRuntime(policy_catalog=catalog)
    runtime.register(manifest, lambda: _Adapter(manifest))
    with pytest.raises(PermissionError, match="trusted activation authority"):
        runtime.activate("research.plugin", approval_refs=("caller-forged",))

    authority = _Authority()
    authorized = PluginRuntime(
        policy_catalog=catalog,
        activation_authority=authority,
    )
    authorized.register(manifest, lambda: _Adapter(manifest))
    authorized.activate(
        "research.plugin",
        approval_refs=("approval://trusted",),
    )
    assert authorized.effective_permissions("research.plugin") == ("workspace.read",)
    subject, _refs = authority.calls[-1]
    assert subject.permission_ids == ("workspace.read",)


def test_plugin_upgrade_is_explicit_compare_and_swap() -> None:
    first = _plugin(permissions=(), actions=())
    second = _plugin(version="2.0.0", permissions=(), actions=())
    runtime = PluginRuntime()
    runtime.register(first, lambda: _Adapter(first))
    with pytest.raises(ValueError, match="expected_version"):
        runtime.upgrade(second, lambda: _Adapter(second), expected_version="0.9.0")
    runtime.upgrade(second, lambda: _Adapter(second), expected_version="1.0.0")
    assert runtime.manifests()["research.plugin"].version == "2.0.0"


@dataclass
class _Dist:
    name: str
    version: str


class _EntryPoint:
    def __init__(
        self,
        name: str,
        value: str,
        loaded: object,
        *,
        distribution_name: str,
    ) -> None:
        self.name = name
        self.value = value
        self.dist = _Dist(distribution_name, "1.0.0")
        self._loaded = loaded

    def load(self) -> object:
        if isinstance(self._loaded, BaseException):
            raise self._loaded
        return self._loaded


def test_discovery_is_metadata_only_provider_neutral_and_load_failures_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _plugin(permissions=(), actions=())
    registration = PluginRegistration(manifest, lambda: _Adapter(manifest))
    good = _EntryPoint(
        "research-plugin",
        "fixture.good:registration",
        registration,
        distribution_name="good-dist",
    )
    bad = _EntryPoint(
        "broken-plugin",
        "fixture.bad:registration",
        RuntimeError("import failed"),
        distribution_name="bad-dist",
    )
    loads = {"count": 0}
    original_good_load = good.load

    def counted_good_load() -> object:
        loads["count"] += 1
        return original_good_load()

    good.load = counted_good_load  # type: ignore[method-assign]
    monkeypatch.setattr(entrypoints, "_entry_points", lambda group: (bad, good))

    discovered = discover_plugin_entrypoints()
    assert loads["count"] == 0
    assert all(type(item) is EntrypointDescriptor for item in discovered)
    assert {item.name for item in discovered} == {"broken-plugin", "research-plugin"}

    report = inspect_plugin_entrypoints(discovered)
    assert loads["count"] == 1
    assert [item[1].manifest.plugin_id for item in report.registrations] == [
        "research.plugin"
    ]
    assert len(report.failures) == 1
    assert report.failures[0].descriptor.name == "broken-plugin"
    assert report.failures[0].error_type == "RuntimeError"


def test_duplicate_discovered_plugin_identity_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _plugin(permissions=(), actions=())
    registration = PluginRegistration(manifest, lambda: _Adapter(manifest))
    first = _EntryPoint(
        "research-plugin",
        "fixture.one:registration",
        registration,
        distribution_name="one-dist",
    )
    second = _EntryPoint(
        "research-plugin-two",
        "fixture.two:registration",
        registration,
        distribution_name="two-dist",
    )
    monkeypatch.setattr(entrypoints, "_entry_points", lambda group: (first, second))
    report = inspect_plugin_entrypoints()
    assert report.registrations == ()
    assert [item.error_type for item in report.failures] == [
        "DuplicatePluginIdentity",
        "DuplicatePluginIdentity",
    ]


def test_public_discovery_annotations_do_not_leak_importlib_metadata() -> None:
    assert "importlib.metadata" not in str(inspect.signature(discover_plugin_entrypoints))
    assert "importlib.metadata" not in str(inspect.signature(discover_workspace_entrypoints))


def test_workspace_catalog_rejects_permission_action_and_plugin_scope_escape() -> None:
    plugin = _plugin()
    catalog = _workspace_catalog()
    catalog.validate(_workspace(version="1.0.0"), {"research.plugin": plugin})

    bad_permission = _workspace(version="1.0.1").model_copy(
        update={"permission_ids": ("workspace.admin",)}
    )
    with pytest.raises(Exception, match="unknown workspace permission"):
        catalog.validate(bad_permission, {"research.plugin": plugin})

    requirement = PluginRequirement(
        plugin_id="research.plugin",
        required_capabilities=("research.capture",),
        required_permission_ids=("workspace.admin",),
    )
    widened = _workspace(version="1.0.2").model_copy(
        update={"required_plugins": (requirement,)}
    )
    with pytest.raises(Exception, match="does not declare permissions"):
        catalog.validate(widened, {"research.plugin": plugin})


def test_workspace_activation_survives_restart_and_rejects_stale_rollback(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    plugin_state = {"manifest": _plugin()}

    def plugins():
        return {"research.plugin": plugin_state["manifest"]}

    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        plugins,
        activation_authority=authority,
    )
    first = repository.save_candidate(_workspace(version="1.0.0"))
    second = repository.save_candidate(_workspace(version="2.0.0"))
    repository.activate(
        second.workspace.workspace_id,
        second.generation,
        approval_refs=("approval://trusted",),
    )
    active_before_restart = repository.active(first.workspace.workspace_id)
    assert active_before_restart is not None
    assert active_before_restart.generation == 2

    restarted = WorkspaceActivationRepository(
        SQLiteStore(store.path),
        _workspace_catalog(),
        plugins,
        activation_authority=authority,
    )
    with pytest.raises(PermissionError, match="stale workspace activation"):
        restarted.activate(
            first.workspace.workspace_id,
            first.generation,
            approval_refs=("approval://trusted",),
        )
    active = restarted.active(first.workspace.workspace_id)
    assert active is not None
    assert active.generation == second.generation
    assert active.workspace.version == "2.0.0"
    assert active.effective_permission_ids == ("workspace.read",)


def test_workspace_activation_requires_fresh_exact_plugin_manifest(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    plugin_state = {"manifest": _plugin()}

    def plugins():
        return {"research.plugin": plugin_state["manifest"]}

    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        plugins,
        activation_authority=authority,
    )
    candidate = repository.save_candidate(_workspace(version="1.0.0"))
    plugin_state["manifest"] = _plugin(version="2.0.0")
    with pytest.raises(PermissionError, match="plugin manifest changed"):
        repository.activate(
            candidate.workspace.workspace_id,
            candidate.generation,
            approval_refs=("approval://trusted",),
        )


def test_workspace_activation_future_schema_fails_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE workspace_activation_schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workspace_activation_schema_migrations(version, applied_at) "
            "VALUES (99, 'future')"
        )
    with pytest.raises(RuntimeError, match="newer"):
        WorkspaceActivationRepository(
            store,
            _workspace_catalog(),
            lambda: {"research.plugin": _plugin()},
        )
