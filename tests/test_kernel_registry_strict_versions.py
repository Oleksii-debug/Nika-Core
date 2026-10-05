from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ніка registry versions.db")
    store.initialize()
    return store


@pytest.mark.parametrize(
    "version", (True, False, 0, -1, 1.5, 2.0, "2", None, 1 << 63, 10**1000)
)
def test_agent_and_workspace_versions_reject_noncanonical_or_unstorable_values(
    version: object,
) -> None:
    with pytest.raises(ValueError, match="positive SQLite-sized integer"):
        AgentDefinition("worker", "Worker", version, "Work")
    with pytest.raises(ValueError, match="positive SQLite-sized integer"):
        WorkspaceDefinition("research", "Research", version)


@pytest.mark.parametrize("enabled", (0, 1, "true", None, [], 2))
def test_workspace_enabled_requires_real_boolean(enabled: object) -> None:
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        WorkspaceDefinition("research", "Research", 1, enabled=enabled)


def test_registry_sqlite_int64_boundary_and_unicode_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    last = (1 << 63) - 1
    agent = AgentDefinition("дослідник", "Дослідник", last, "Перевіряти")
    workspace = WorkspaceDefinition("завдання", "Завдання", last, enabled=False)
    agents = AgentRegistry(store)
    spaces = WorkspaceRegistry(store)
    agents.register(agent)
    spaces.register(workspace)
    assert agents.get(agent.agent_id) == agent
    assert agents.list_latest() == (agent,)
    assert spaces.get(workspace.workspace_id) == workspace
    assert spaces.list_latest() == (workspace,)
    with pytest.raises(ValueError, match="agent version must increase"):
        agents.register(agent)
    with pytest.raises(ValueError, match="workspace version must increase"):
        spaces.register(workspace)


@pytest.mark.parametrize("version", (1.5, "bad", -1))
def test_corrupt_agent_version_rejected_on_all_authority_reads(
    tmp_path: Path, version: object
) -> None:
    store = _store(tmp_path)
    registry = AgentRegistry(store)
    registry.register(AgentDefinition("worker", "Worker", 1, "Work"))
    with store.connection() as conn:
        conn.execute("UPDATE agents SET version = ? WHERE agent_id = ?", (version, "worker"))
    with pytest.raises(ValueError, match="invalid persisted agent version"):
        registry.get("worker")
    with pytest.raises(ValueError, match="invalid persisted agent version"):
        registry.list_latest()
    with pytest.raises(ValueError, match="invalid persisted agent version"):
        registry.register(AgentDefinition("worker", "Worker", 2, "Next"))


@pytest.mark.parametrize("version", (1.5, "bad", -1))
def test_corrupt_workspace_version_rejected_on_all_authority_reads(
    tmp_path: Path, version: object
) -> None:
    store = _store(tmp_path)
    registry = WorkspaceRegistry(store)
    registry.register(WorkspaceDefinition("research", "Research", 1))
    with store.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET version = ? WHERE workspace_id = ?",
            (version, "research"),
        )
    with pytest.raises(ValueError, match="invalid persisted workspace version"):
        registry.get("research")
    with pytest.raises(ValueError, match="invalid persisted workspace version"):
        registry.list_latest()
    with pytest.raises(ValueError, match="invalid persisted workspace version"):
        registry.register(WorkspaceDefinition("research", "Research", 2))


def test_corrupt_workspace_enabled_does_not_become_truthy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = WorkspaceRegistry(store)
    registry.register(WorkspaceDefinition("research", "Research", 1, enabled=True))
    with store.connection() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE workspaces SET enabled = ? WHERE workspace_id = ?",
            ("invalid", "research"),
        )
    with pytest.raises(ValueError, match="invalid persisted workspace enabled flag"):
        registry.get("research")
    with pytest.raises(ValueError, match="invalid persisted workspace enabled flag"):
        registry.list_latest()
