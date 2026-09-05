from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.activation_authority import ActivationSubject
from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
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
    entrypoints,
    inspect_plugin_entrypoints,
)
from nika_core.tools import ToolRisk, ToolSpec
from nika_core.workspaces import (
    PluginRequirement,
    WorkspaceActivationRepository,
    WorkspaceCatalog,
    WorkspaceManifest,
    WorkspacePolicyCatalog,
)


class _Authority:
    def __init__(self) -> None:
        self.calls: list[ActivationSubject] = []

    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None:
        self.calls.append(subject)
        if approval_refs != ("approval://trusted",):
            raise PermissionError("untrusted approval evidence")


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _plugin(*, version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(
        plugin_id="research.plugin",
        name="Research plugin",
        version=version,
        entrypoint_name="research-plugin",
        capabilities=(
            CapabilityDeclaration(
                capability_id="research.capture",
                description="Capture research evidence",
            ),
        ),
        permission_ids=("workspace.read", "workspace.write"),
        action_ids=("workspace.open",),
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
    )


def _workspace_catalog() -> WorkspaceCatalog:
    return WorkspaceCatalog(
        WorkspacePolicyCatalog(
            permission_ids=frozenset({"workspace.read", "workspace.write"}),
            action_ids=frozenset({"workspace.open"}),
        )
    )


def _dangerous_agent() -> tuple[AgentDefinition, AgentCompiler]:
    tool = ToolSpec("release.publish", "Publish a release", ToolRisk.HIGH_IMPACT)
    definition = AgentDefinition(
        agent_id="release.agent",
        name="Release agent",
        goal="Publish a reviewed release",
        instructions="Use only the reviewed release capability.",
        model_profile="local-default",
        tool_grants=(
            ToolGrant(
                tool_id="release.publish",
                max_risk=RiskTier.R4_HIGH_IMPACT,
            ),
        ),
    )
    compiler = AgentCompiler(
        tools=(tool,),
        model_profiles={"local-default"},
    )
    return definition, compiler


def test_plugin_permission_subset_is_explicit_and_cannot_widen_while_active() -> None:
    manifest = _plugin()
    authority = _Authority()
    runtime = PluginRuntime(
        policy_catalog=PluginPolicyCatalog(
            permission_ids=frozenset({"workspace.read", "workspace.write"}),
            action_ids=frozenset({"workspace.open"}),
        ),
        activation_authority=authority,
    )
    runtime.register(manifest, lambda: _Adapter(manifest))

    with pytest.raises(PermissionError, match="explicit plugin permission selection"):
        runtime.activate("research.plugin", approval_refs=("approval://trusted",))
    with pytest.raises(PermissionError, match="undeclared permissions"):
        runtime.activate(
            "research.plugin",
            permission_ids=("workspace.admin",),
            approval_refs=("approval://trusted",),
        )

    adapter = runtime.activate(
        "research.plugin",
        permission_ids=("workspace.read",),
        approval_refs=("approval://trusted",),
    )
    assert runtime.effective_permissions("research.plugin") == ("workspace.read",)
    assert authority.calls[-1].permission_ids == ("workspace.read",)

    with pytest.raises(PermissionError, match="active plugin permission set differs"):
        runtime.activate(
            "research.plugin",
            permission_ids=("workspace.read", "workspace.write"),
            approval_refs=("approval://trusted",),
        )
    assert runtime.effective_permissions("research.plugin") == ("workspace.read",)

    runtime.deactivate("research.plugin")
    assert adapter.closed is True


def test_plugin_policy_catalog_rejects_unknown_manifest_ids() -> None:
    bad = _plugin().model_copy(update={"permission_ids": ("workspace.admin",)})
    runtime = PluginRuntime(
        policy_catalog=PluginPolicyCatalog(
            permission_ids=frozenset({"workspace.read"}),
            action_ids=frozenset({"workspace.open"}),
        )
    )
    with pytest.raises(PluginCompatibilityError, match="unknown plugin permission"):
        runtime.register(bad, lambda: _Adapter(bad))


@dataclass
class _Dist:
    name: str
    version: str


class _EntryPoint:
    def __init__(self, name: str, loaded: object, distribution_name: str) -> None:
        self.name = name
        self.value = f"fixture.{name}:registration"
        self.dist = _Dist(distribution_name, "1.0.0")
        self._loaded = loaded
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        if isinstance(self._loaded, BaseException):
            raise self._loaded
        return self._loaded


def test_discovery_is_metadata_only_provider_neutral_and_failure_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _plugin().model_copy(update={"permission_ids": (), "action_ids": ()})
    registration = PluginRegistration(manifest, lambda: _Adapter(manifest))
    good = _EntryPoint("research-plugin", registration, "good-dist")
    bad = _EntryPoint("broken-plugin", RuntimeError("import failed"), "bad-dist")
    monkeypatch.setattr(entrypoints, "_entry_points", lambda _group: (bad, good))

    discovered = discover_plugin_entrypoints()
    assert good.loads == 0
    assert bad.loads == 0
    assert all(isinstance(item, EntrypointDescriptor) for item in discovered)

    report = inspect_plugin_entrypoints(discovered)
    assert good.loads == 1
    assert bad.loads == 1
    assert len(report.registrations) == 1
    assert len(report.failures) == 1
    assert report.failures[0].error_type == "RuntimeError"
    assert "importlib.metadata" not in str(inspect.signature(discover_plugin_entrypoints))
    assert "importlib.metadata" not in str(inspect.signature(discover_workspace_entrypoints))


def test_workspace_restart_blocks_stale_activation_and_keeps_newer_version(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    manifest = _plugin()

    def plugins() -> dict[str, PluginManifest]:
        return {"research.plugin": manifest}

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
    assert active.effective_permission_ids == ("workspace.read",)


def test_workspace_activation_rejects_persisted_permission_widening(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    manifest = _plugin()
    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        lambda: {"research.plugin": manifest},
        activation_authority=authority,
    )
    candidate = repository.save_candidate(_workspace(version="1.0.0"))
    with store.connection() as conn:
        conn.execute(
            "UPDATE workspace_activation_versions "
            "SET effective_permissions_json = ? "
            "WHERE workspace_id = ? AND generation = ?",
            (
                json.dumps(["workspace.read", "workspace.write"]),
                candidate.workspace.workspace_id,
                candidate.generation,
            ),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="differ from manifest selection"):
        repository.activate(
            candidate.workspace.workspace_id,
            candidate.generation,
            approval_refs=("approval://trusted",),
        )
    assert repository.active(candidate.workspace.workspace_id) is None
    assert authority.calls == []


def test_workspace_activation_rejects_plugin_manifest_drift(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    state = {"manifest": _plugin()}

    def plugins() -> dict[str, PluginManifest]:
        return {"research.plugin": state["manifest"]}

    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        plugins,
        activation_authority=_Authority(),
    )
    candidate = repository.save_candidate(_workspace(version="1.0.0"))
    state["manifest"] = _plugin(version="2.0.0")
    with pytest.raises(PermissionError, match="plugin manifest changed"):
        repository.activate(
            candidate.workspace.workspace_id,
            candidate.generation,
            approval_refs=("approval://trusted",),
        )


def test_agent_activation_rejects_persisted_r4_approval_downgrade(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    definition, compiler = _dangerous_agent()
    repository = AgentDefinitionRepository(store, activation_authority=authority)
    repository.save_draft(compiler.compile(definition))
    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET required_approvals_json = '[]' "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )
        conn.commit()

    with pytest.raises(PermissionError, match="high-impact approval metadata"):
        repository.activate(definition, approval_refs=("approval://trusted",))
    assert repository.active(definition.agent_id) is None
    assert authority.calls == []


def test_agent_activation_rejects_persisted_risk_downgrade(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    definition, compiler = _dangerous_agent()
    repository = AgentDefinitionRepository(store, activation_authority=authority)
    repository.save_draft(compiler.compile(definition))
    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET highest_risk = 0 "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )
        conn.commit()

    with pytest.raises(PermissionError, match="risk metadata"):
        repository.activate(definition, approval_refs=("approval://trusted",))
    assert repository.active(definition.agent_id) is None
    assert authority.calls == []


def test_workspace_active_read_rejects_persisted_permission_widening(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifest = _plugin()
    authority = _Authority()
    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        lambda: {"research.plugin": manifest},
        activation_authority=authority,
    )
    candidate = repository.save_candidate(_workspace(version="1.0.0"))
    repository.activate(
        candidate.workspace.workspace_id,
        candidate.generation,
        approval_refs=("approval://trusted",),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE workspace_activation_versions "
            "SET effective_permissions_json = ? "
            "WHERE workspace_id = ? AND generation = ?",
            (
                json.dumps(["workspace.read", "workspace.write"]),
                candidate.workspace.workspace_id,
                candidate.generation,
            ),
        )
        conn.commit()

    restarted = WorkspaceActivationRepository(
        SQLiteStore(store.path),
        _workspace_catalog(),
        lambda: {"research.plugin": manifest},
        activation_authority=authority,
    )
    with pytest.raises(RuntimeError, match="differ from manifest selection"):
        restarted.active(candidate.workspace.workspace_id)


def test_workspace_activation_rejects_corrupt_reviewed_plugin_set(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifest = _plugin()
    authority = _Authority()
    repository = WorkspaceActivationRepository(
        store,
        _workspace_catalog(),
        lambda: {"research.plugin": manifest},
        activation_authority=authority,
    )
    candidate = repository.save_candidate(_workspace(version="1.0.0"))
    with store.connection() as conn:
        conn.execute(
            "UPDATE workspace_activation_versions SET plugins_json = '[]' "
            "WHERE workspace_id = ? AND generation = ?",
            (candidate.workspace.workspace_id, candidate.generation),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="plugin review set"):
        repository.activate(
            candidate.workspace.workspace_id,
            candidate.generation,
            approval_refs=("approval://trusted",),
        )
    assert authority.calls == []


def test_agent_active_read_rejects_persisted_r4_metadata_downgrade(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _Authority()
    definition, compiler = _dangerous_agent()
    repository = AgentDefinitionRepository(store, activation_authority=authority)
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition, approval_refs=("approval://trusted",))

    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET required_approvals_json = '[]' "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )
        conn.commit()

    restarted = AgentDefinitionRepository(
        SQLiteStore(store.path),
        activation_authority=authority,
    )
    with pytest.raises(PermissionError, match="high-impact approval metadata"):
        restarted.active(definition.agent_id)


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
        conn.commit()
    with pytest.raises(RuntimeError, match="newer"):
        WorkspaceActivationRepository(
            store,
            _workspace_catalog(),
            lambda: {"research.plugin": _plugin()},
        )
