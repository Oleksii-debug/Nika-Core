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
    with pytest.raises(ValueError, match="invalid persisted workspace enabled flag"):
        registry.register(WorkspaceDefinition("research", "Research", 2))


class _SpoofedText(str):
    def strip(self, chars: str | None = None) -> str:
        return "spoofed-nonempty"


@pytest.mark.parametrize("value", (b"worker", 7, _SpoofedText("")))
def test_agent_id_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="agent_id must be text"):
        AgentDefinition(value, "Worker", 1, "Work")


@pytest.mark.parametrize("value", (b"Worker", 7, _SpoofedText("Worker")))
def test_agent_name_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="name must be text"):
        AgentDefinition("worker", value, 1, "Work")


@pytest.mark.parametrize("value", (b"Work", 7, _SpoofedText("Work")))
def test_agent_goal_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="goal must be text"):
        AgentDefinition("worker", "Worker", 1, value)


@pytest.mark.parametrize("value", (b"research", 7, _SpoofedText("")))
def test_workspace_id_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="workspace_id must be text"):
        WorkspaceDefinition(value, "Research", 1)


@pytest.mark.parametrize("value", (b"Research", 7, _SpoofedText("Research")))
def test_workspace_name_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="name must be text"):
        WorkspaceDefinition("research", value, 1)


@pytest.mark.parametrize("value", (b"Description", 7, _SpoofedText("Description")))
def test_workspace_description_requires_exact_builtin_text(value: object) -> None:
    with pytest.raises(ValueError, match="description must be text"):
        WorkspaceDefinition("research", "Research", 1, description=value)


def test_registry_definitions_reject_non_utf8_unicode() -> None:
    invalid = chr(0xD800)
    with pytest.raises(ValueError, match="agent_id must be valid UTF-8 text"):
        AgentDefinition(invalid, "Worker", 1, "Work")
    with pytest.raises(ValueError, match="workspace_id must be valid UTF-8 text"):
        WorkspaceDefinition(invalid, "Research", 1)


def test_registry_lookups_reject_noncanonical_text_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    agents = AgentRegistry(store)
    spaces = WorkspaceRegistry(store)
    agents.register(AgentDefinition("worker", "Worker", 1, "Work"))
    spaces.register(WorkspaceDefinition("research", "Research", 1))

    for value in (b"worker", _SpoofedText("worker")):
        with pytest.raises(ValueError, match="agent_id must be text"):
            agents.get(value)
    for value in (b"research", _SpoofedText("research")):
        with pytest.raises(ValueError, match="workspace_id must be text"):
            spaces.get(value)


def test_corrupt_agent_name_fails_reads_and_successor_registration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = AgentRegistry(store)
    registry.register(AgentDefinition("worker", "Worker", 1, "Work"))
    with store.connection() as conn:
        conn.execute(
            "UPDATE agents SET name = ? WHERE agent_id = ?",
            (b"Worker", "worker"),
        )

    with pytest.raises(ValueError, match="invalid persisted agent name"):
        registry.get("worker")
    with pytest.raises(ValueError, match="invalid persisted agent name"):
        registry.list_latest()
    with pytest.raises(ValueError, match="invalid persisted agent name"):
        registry.register(AgentDefinition("worker", "Worker", 2, "Next"))


def test_corrupt_workspace_description_fails_reads_and_successor_registration(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = WorkspaceRegistry(store)
    registry.register(
        WorkspaceDefinition("research", "Research", 1, description="Stable")
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET description = ? WHERE workspace_id = ?",
            (b"Stable", "research"),
        )

    with pytest.raises(ValueError, match="invalid persisted workspace description"):
        registry.get("research")
    with pytest.raises(ValueError, match="invalid persisted workspace description"):
        registry.list_latest()
    with pytest.raises(ValueError, match="invalid persisted workspace description"):
        registry.register(WorkspaceDefinition("research", "Research", 2))


def test_corrupt_registry_identity_storage_class_fails_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    agents = AgentRegistry(store)
    spaces = WorkspaceRegistry(store)
    agents.register(AgentDefinition("worker", "Worker", 1, "Work"))
    spaces.register(WorkspaceDefinition("research", "Research", 1))
    with store.connection() as conn:
        conn.execute(
            "UPDATE agents SET agent_id = ? WHERE agent_id = ?",
            (b"worker", "worker"),
        )
        conn.execute(
            "UPDATE workspaces SET workspace_id = ? WHERE workspace_id = ?",
            (b"research", "research"),
        )

    with pytest.raises(ValueError, match="invalid persisted agent agent_id"):
        agents.list_latest()
    with pytest.raises(ValueError, match="invalid persisted workspace workspace_id"):
        spaces.list_latest()


def test_tampered_agent_definition_is_readmitted_before_sqlite_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = AgentRegistry(store)
    definition = AgentDefinition("worker", "Worker", 1, "Work")
    object.__setattr__(definition, "version", True)

    with pytest.raises(ValueError, match="positive SQLite-sized integer"):
        registry.register(definition)

    assert registry.count == 0


def test_tampered_workspace_definition_is_readmitted_before_sqlite_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = WorkspaceRegistry(store)
    definition = WorkspaceDefinition("research", "Research", 1)
    object.__setattr__(definition, "enabled", 1)

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        registry.register(definition)

    assert registry.count == 0


def test_tampered_definition_text_is_rejected_before_durable_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    registry = AgentRegistry(store)
    definition = AgentDefinition("worker", "Worker", 1, "Work")
    object.__setattr__(definition, "goal", _SpoofedText("forged"))

    with pytest.raises(ValueError, match="goal must be text"):
        registry.register(definition)

    assert registry.count == 0


def test_in_memory_agent_registry_detaches_input_and_read_aliases() -> None:
    registry = AgentRegistry()
    definition = AgentDefinition("worker", "Worker", 1, "Work")
    registry.register(definition)

    object.__setattr__(definition, "name", "caller-mutated")
    assert registry.get("worker").name == "Worker"

    returned = registry.get("worker")
    object.__setattr__(returned, "goal", "read-alias-mutated")
    assert registry.get("worker").goal == "Work"

    listed = registry.list_latest()[0]
    object.__setattr__(listed, "version", 0)
    assert registry.get("worker").version == 1
