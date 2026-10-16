from __future__ import annotations

import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


class _CoordinatedAgentRegistry(AgentRegistry):
    """Force the legacy pre-transaction latest checks to observe the same version."""

    def __init__(self, store: SQLiteStore, barrier: Barrier) -> None:
        super().__init__(store)
        self._barrier = barrier

    def _latest(self, agent_id: str) -> AgentDefinition | None:
        current = super()._latest(agent_id)
        self._barrier.wait(timeout=5)
        return current


class _CoordinatedWorkspaceRegistry(WorkspaceRegistry):
    """Force the legacy pre-transaction latest checks to observe the same version."""

    def __init__(self, store: SQLiteStore, barrier: Barrier) -> None:
        super().__init__(store)
        self._barrier = barrier

    def _latest(self, workspace_id: str) -> WorkspaceDefinition | None:
        current = super()._latest(workspace_id)
        self._barrier.wait(timeout=5)
        return current


def _assert_one_success_one_contract_rejection(futures: list[Future[None]]) -> None:
    outcomes: list[str] = []
    for future in futures:
        try:
            future.result(timeout=10)
        except ValueError:
            outcomes.append("rejected")
        except sqlite3.IntegrityError as exc:
            pytest.fail(f"registry leaked sqlite integrity error: {exc}")
        else:
            outcomes.append("registered")
    assert sorted(outcomes) == ["registered", "rejected"]


def test_agent_registry_serializes_duplicate_version_writers(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent-concurrency.db")
    store.initialize()
    AgentRegistry(store).register(AgentDefinition("worker", "Worker", 1, "Initial"))

    barrier = Barrier(2)
    registries = [
        _CoordinatedAgentRegistry(store, barrier),
        _CoordinatedAgentRegistry(store, barrier),
    ]
    definition = AgentDefinition("worker", "Worker", 2, "Updated")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(registry.register, definition) for registry in registries]
        _assert_one_success_one_contract_rejection(futures)

    assert AgentRegistry(store).get("worker").version == 2
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM agents WHERE agent_id = ? AND version = ?",
            ("worker", 2),
        ).fetchone()["count"]
    assert int(count) == 1


def test_workspace_registry_serializes_duplicate_version_writers(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "workspace-concurrency.db")
    store.initialize()
    WorkspaceRegistry(store).register(WorkspaceDefinition("research", "Research", 1))

    barrier = Barrier(2)
    registries = [
        _CoordinatedWorkspaceRegistry(store, barrier),
        _CoordinatedWorkspaceRegistry(store, barrier),
    ]
    definition = WorkspaceDefinition("research", "Research", 2, "Updated")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(registry.register, definition) for registry in registries]
        _assert_one_success_one_contract_rejection(futures)

    assert WorkspaceRegistry(store).get("research").version == 2
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM workspaces WHERE workspace_id = ? AND version = ?",
            ("research", 2),
        ).fetchone()["count"]
    assert int(count) == 1
