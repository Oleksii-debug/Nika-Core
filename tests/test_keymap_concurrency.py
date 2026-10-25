from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap


class _ConflictBarrierKeymap(Keymap):
    """Force the legacy read-before-write path to expose its TOCTOU race."""

    def __init__(self, store: SQLiteStore, actions: ActionRegistry, barrier: Barrier) -> None:
        super().__init__(store, actions)
        self._barrier = barrier

    def conflict(self, action_id: str, binding: str | None) -> str | None:
        result = super().conflict(action_id, binding)
        self._barrier.wait(timeout=5)
        return result


def _registry() -> ActionRegistry:
    actions = ActionRegistry()
    actions.register(
        ActionDefinition(
            action_id="test.first",
            label="First",
            category="Test",
            default_binding="Ctrl+1",
        )
    )
    actions.register(
        ActionDefinition(
            action_id="test.second",
            label="Second",
            category="Test",
            default_binding="Ctrl+2",
        )
    )
    return actions


def test_concurrent_writers_cannot_commit_same_binding(tmp_path) -> None:
    database = tmp_path / "nika.db"
    SQLiteStore(database).initialize()
    actions = _registry()
    barrier = Barrier(2)
    first = _ConflictBarrierKeymap(SQLiteStore(database), actions, barrier)
    second = _ConflictBarrierKeymap(SQLiteStore(database), actions, barrier)

    def bind(keymap: Keymap, action_id: str) -> str:
        try:
            keymap.set_binding(action_id, "Ctrl+K")
        except ValueError:
            return "conflict"
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = (
            executor.submit(bind, first, "test.first"),
            executor.submit(bind, second, "test.second"),
        )
        statuses = sorted(result.result(timeout=10) for result in results)

    assert statuses == ["conflict", "ok"]
    persisted = Keymap(SQLiteStore(database), actions)
    assert [persisted.resolve("test.first"), persisted.resolve("test.second")].count("Ctrl+K") == 1


def test_restore_default_rejects_new_conflict_and_preserves_override(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())
    keymap.set_binding("test.first", "Ctrl+9")
    keymap.set_binding("test.second", "Ctrl+1")

    with pytest.raises(ValueError, match="shortcut conflict with test.second"):
        keymap.restore_default("test.first")

    assert keymap.resolve("test.first") == "Ctrl+9"
    assert keymap.resolve("test.second") == "Ctrl+1"


def test_import_conflict_rolls_back_all_proposed_bindings(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())
    payload = """{
      "format_version": 1,
      "bindings": {
        "test.first": "Ctrl+K",
        "test.second": "Ctrl+K"
      }
    }"""

    with pytest.raises(ValueError, match="shortcut conflict between test.first and test.second"):
        keymap.import_json(payload)

    assert keymap.resolve("test.first") == "Ctrl+1"
    assert keymap.resolve("test.second") == "Ctrl+2"


def test_set_binding_canonicalizes_modifier_aliases_and_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())

    keymap.set_binding("test.first", "Shift+Control+K")

    assert keymap.resolve("test.first") == "Ctrl+Shift+K"
    with store.connection() as conn:
        row = conn.execute(
            "SELECT binding FROM keymap_overrides WHERE action_id = ?", ("test.first",)
        ).fetchone()
    assert row is not None
    assert row["binding"] == "Ctrl+Shift+K"


def test_import_canonicalizes_modifier_aliases_and_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())
    payload = json.dumps(
        {
            "format_version": 1,
            "bindings": {
                "test.first": "Super+Alt+Control+K",
                "test.second": None,
            },
        }
    )

    keymap.import_json(payload)

    assert keymap.resolve("test.first") == "Ctrl+Alt+Win+K"
    assert keymap.resolve("test.second") is None


def test_legacy_noncanonical_override_resolves_and_exports_canonically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?)",
            ("test.first", "Shift+Control+K", "legacy"),
        )

    assert keymap.resolve("test.first") == "Ctrl+Shift+K"
    exported = json.loads(keymap.export_json())
    assert exported["bindings"]["test.first"] == "Ctrl+Shift+K"


def test_noncanonical_default_binding_resolves_canonically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = ActionRegistry()
    actions.register(
        ActionDefinition(
            action_id="test.default",
            label="Default",
            category="Test",
            default_binding="Shift+Control+K",
        )
    )

    assert Keymap(store, actions).resolve("test.default") == "Ctrl+Shift+K"


def test_duplicate_modifier_aliases_remain_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    keymap = Keymap(store, _registry())

    with pytest.raises(ValueError, match="duplicate shortcut modifier"):
        keymap.set_binding("test.first", "Ctrl+Control+K")
