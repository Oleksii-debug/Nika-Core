from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "registry identity alias.db")
    store.initialize()
    return store


def test_agent_registry_rejects_blob_identity_alias_for_lookup_count_and_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = AgentRegistry(store)
    registry.register(AgentDefinition("worker", "Worker", 1, "Work"))

    with store.connection() as conn:
        conn.execute(
            "UPDATE agents SET agent_id = ? WHERE agent_id = ?",
            (b"worker", "worker"),
        )

    with pytest.raises(ValueError, match="invalid persisted agent agent_id"):
        registry.get("worker")
    with pytest.raises(ValueError, match="invalid persisted agent agent_id"):
        _ = registry.count
    with pytest.raises(ValueError, match="invalid persisted agent agent_id"):
        registry.register(AgentDefinition("worker", "Worker", 2, "Next"))

    with store.connection() as conn:
        rows = conn.execute("SELECT typeof(agent_id), version FROM agents").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("blob", 1)]


def test_workspace_registry_rejects_blob_identity_alias_for_lookup_count_and_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = WorkspaceRegistry(store)
    registry.register(WorkspaceDefinition("research", "Research", 1))

    with store.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET workspace_id = ? WHERE workspace_id = ?",
            (b"research", "research"),
        )

    with pytest.raises(ValueError, match="invalid persisted workspace workspace_id"):
        registry.get("research")
    with pytest.raises(ValueError, match="invalid persisted workspace workspace_id"):
        _ = registry.count
    with pytest.raises(ValueError, match="invalid persisted workspace workspace_id"):
        registry.register(WorkspaceDefinition("research", "Research", 2))

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT typeof(workspace_id), version FROM workspaces"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [("blob", 1)]


def test_registry_count_rejects_unrelated_nontext_identity_corruption(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    agents = AgentRegistry(store)
    spaces = WorkspaceRegistry(store)
    agents.register(AgentDefinition("worker", "Worker", 1, "Work"))
    spaces.register(WorkspaceDefinition("research", "Research", 1))

    with store.connection() as conn:
        conn.execute(
            "UPDATE agents SET agent_id = ? WHERE agent_id = ?",
            (b"other-worker", "worker"),
        )
        conn.execute(
            "UPDATE workspaces SET workspace_id = ? WHERE workspace_id = ?",
            (b"other-research", "research"),
        )

    with pytest.raises(ValueError, match="invalid persisted agent agent_id"):
        _ = agents.count
    with pytest.raises(ValueError, match="invalid persisted workspace workspace_id"):
        _ = spaces.count
