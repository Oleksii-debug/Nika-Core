from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


def test_agent_registry_holds_write_reservation_during_version_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = SQLiteStore(tmp_path / "agent registry.db")
    store.initialize()
    registry = AgentRegistry(store)
    registry.register(AgentDefinition("researcher", "Researcher", 1, "Find sources"))

    validation_reached = threading.Event()
    release_validation = threading.Event()
    errors: list[BaseException] = []
    original_latest = registry._latest

    def paused_latest(
        agent_id: str, *, conn: sqlite3.Connection | None = None
    ) -> AgentDefinition | None:
        if conn is None:
            current = original_latest(agent_id)
        else:
            current = original_latest(agent_id, conn=conn)
        validation_reached.set()
        if not release_validation.wait(timeout=5):
            raise TimeoutError("test did not release agent version validation")
        return current

    monkeypatch.setattr(registry, "_latest", paused_latest)

    def register_next() -> None:
        try:
            registry.register(
                AgentDefinition("researcher", "Researcher", 2, "Find and verify sources")
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=register_next, daemon=True)
    worker.start()
    assert validation_reached.wait(timeout=5)

    competitor = sqlite3.connect(store.path, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            competitor.execute("BEGIN IMMEDIATE")
    finally:
        competitor.close()
        release_validation.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert registry.get("researcher").version == 2
    with pytest.raises(ValueError, match="agent version must increase"):
        registry.register(AgentDefinition("researcher", "Researcher", 1, "Stale"))


def test_workspace_registry_holds_write_reservation_during_version_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = SQLiteStore(tmp_path / "робочий простір registry.db")
    store.initialize()
    registry = WorkspaceRegistry(store)
    registry.register(WorkspaceDefinition("research", "Research", 1, "Initial"))

    validation_reached = threading.Event()
    release_validation = threading.Event()
    errors: list[BaseException] = []
    original_latest = registry._latest

    def paused_latest(
        workspace_id: str, *, conn: sqlite3.Connection | None = None
    ) -> WorkspaceDefinition | None:
        if conn is None:
            current = original_latest(workspace_id)
        else:
            current = original_latest(workspace_id, conn=conn)
        validation_reached.set()
        if not release_validation.wait(timeout=5):
            raise TimeoutError("test did not release workspace version validation")
        return current

    monkeypatch.setattr(registry, "_latest", paused_latest)

    def register_next() -> None:
        try:
            registry.register(WorkspaceDefinition("research", "Research", 2, "Improved"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=register_next, daemon=True)
    worker.start()
    assert validation_reached.wait(timeout=5)

    competitor = sqlite3.connect(store.path, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            competitor.execute("BEGIN IMMEDIATE")
    finally:
        competitor.close()
        release_validation.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert registry.get("research").version == 2
    with pytest.raises(ValueError, match="workspace version must increase"):
        registry.register(WorkspaceDefinition("research", "Research", 1, "Stale"))
