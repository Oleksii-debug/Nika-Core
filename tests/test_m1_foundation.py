from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.workspace_registry import WorkspaceDefinition, WorkspaceRegistry


def test_config_uses_nika_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NIKA_DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("NIKA_LOG_LEVEL", "debug")
    monkeypatch.setenv("NIKA_MODEL_PROVIDER", "OLLAMA")
    config = AppConfig.from_environment()
    assert config.database_path == tmp_path / "custom.db"
    assert config.log_level == "DEBUG"
    assert config.model_provider == "ollama"


def test_config_accepts_explicit_database_path_and_long_env_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "explicit.db"
    assert AppConfig(database_path=explicit).database_path == explicit
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(tmp_path / "long-name.db"))
    assert AppConfig.from_environment().database_path == tmp_path / "long-name.db"


def test_legacy_db_path_has_priority_when_both_env_names_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NIKA_DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(tmp_path / "long-name.db"))
    assert AppConfig.from_environment().database_path == tmp_path / "legacy.db"


def test_invalid_config_fails_closed() -> None:
    with pytest.raises(ValueError):
        AppConfig(log_level="verbose")


def test_schema_migrates_existing_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'old')")
    conn.execute(
        "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
        "agent_id TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.initialize()
    assert store.schema_version() == 7
    with store.connection() as check:
        names = {row["name"] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "agents",
        "workspaces",
        "audit_events",
        "keymap_overrides",
        "runtime_sessions",
        "idempotency_records",
        "memory_records",
        "scheduled_jobs",
        "resource_budgets",
        "agent_definitions",
        "multi_agent_teams",
        "multi_agent_members",
        "multi_agent_handoffs",
        "multi_agent_results",
        "experiments",
        "experiment_observations",
        "experiment_events",
    } <= names


def test_future_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'future')")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        SQLiteStore(path).initialize()


def test_agent_registry_persists_versions(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    first = AgentRegistry(store)
    first.register(AgentDefinition("researcher", "Researcher", 1, "Find sources"))
    second = AgentRegistry(store)
    assert second.get("researcher").goal == "Find sources"
    second.register(AgentDefinition("researcher", "Researcher", 2, "Find and verify sources"))
    assert first.get("researcher").version == 2
    with pytest.raises(ValueError):
        first.register(AgentDefinition("researcher", "Researcher", 2, "Duplicate"))


def test_workspace_registry_persists_latest_versions(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    registry = WorkspaceRegistry(store)
    registry.register(WorkspaceDefinition("research", "Research", 1, "Initial"))
    registry.register(WorkspaceDefinition("research", "Research", 2, "Improved"))
    registry.register(WorkspaceDefinition("youtube", "YouTube", 1))
    assert registry.count == 2
    assert registry.get("research").description == "Improved"
    assert [item.workspace_id for item in registry.list_latest()] == ["research", "youtube"]


def test_audit_log_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    event_id = audit.append(
        event_type="workspace.registered",
        entity_type="workspace",
        entity_id="research",
        payload={"version": 1},
    )
    events = audit.list_for(entity_type="workspace", entity_id="research")
    assert events[0].event_id == event_id
    assert events[0].payload == {"version": 1}


def test_action_registry_rejects_duplicate_ids_and_default_shortcuts() -> None:
    registry = ActionRegistry()
    registry.register(ActionDefinition("task.create", "Create", "Tasks", "Ctrl+N"))
    with pytest.raises(ValueError):
        registry.register(ActionDefinition("task.create", "Duplicate", "Tasks", "Ctrl+M"))
    with pytest.raises(ValueError):
        registry.register(ActionDefinition("task.other", "Other", "Tasks", "ctrl + n"))


def test_action_registry_normalizes_modifier_order_and_aliases() -> None:
    registry = ActionRegistry()
    registry.register(ActionDefinition("agent.stop", "Stop", "Agents", "Ctrl+Shift+S"))
    with pytest.raises(ValueError):
        registry.register(ActionDefinition("agent.other", "Other", "Agents", "shift+control+s"))


def test_action_registry_rejects_ambiguous_or_duplicate_modifier_bindings() -> None:
    with pytest.raises(ValueError):
        ActionDefinition("task.bad", "Bad", "Tasks", "Ctrl+A+B")
    with pytest.raises(ValueError):
        ActionDefinition("task.bad", "Bad", "Tasks", "Ctrl+Control+N")


def test_keymap_remap_clear_restore_and_persist(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    assert keymap.resolve("task.create") == "Ctrl+N"
    keymap.set_binding("task.pause", "Ctrl+Alt+P")
    assert Keymap(store, actions).resolve("task.pause") == "Ctrl+Alt+P"
    keymap.set_binding("task.pause", None)
    assert keymap.resolve("task.pause") is None
    keymap.restore_default("task.pause")
    assert keymap.resolve("task.pause") == "Ctrl+P"
    with pytest.raises(ValueError):
        keymap.set_binding("task.create", None)


def test_keymap_conflicts_and_import_are_atomic(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    with pytest.raises(ValueError):
        keymap.set_binding("task.pause", "Ctrl+N")
    original = keymap.export_json()
    bad = original.replace('"Ctrl+P"', '"Ctrl+N"')
    with pytest.raises(ValueError):
        keymap.import_json(bad)
    assert keymap.export_json() == original


def test_keymap_detects_equivalent_reordered_shortcut(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    with pytest.raises(ValueError):
        keymap.set_binding("task.pause", "shift+ctrl+s")
