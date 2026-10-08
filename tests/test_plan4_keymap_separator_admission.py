"""Plan 4 Section 1: keymap admission cannot poison NVDA action readback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_line_separators_never_persist_through_direct_keymap_write_or_import(
    tmp_path: Path, separator: str
) -> None:
    db_path = tmp_path / "keyboard.sqlite"
    store = SQLiteStore(db_path)
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    initial_tasks = keymap.resolve("nav.tasks")
    initial_agents = keymap.resolve("nav.agents")

    # Persisted keymap mutations have to use the same text admission as the
    # screen-reader bridge. A bad direct caller must not poison list_actions.
    with pytest.raises(ValueError, match="unsafe control"):
        keymap.set_binding("nav.tasks", f"Alt+8{separator}")
    assert keymap.resolve("nav.tasks") == initial_tasks

    # An import must validate every proposed binding before committing *any*.
    # The valid sibling is intentionally ordered before the invalid one.
    imported = json.dumps(
        {
            "format_version": Keymap.FORMAT_VERSION,
            "bindings": {
                "nav.agents": "Alt+9",
                "nav.tasks": f"Alt+8{separator}",
            },
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValueError, match="unsafe control"):
        keymap.import_json(imported)
    assert keymap.resolve("nav.agents") == initial_agents
    assert keymap.resolve("nav.tasks") == initial_tasks

    # The same protection applies when a plugin attempts to register a default.
    with pytest.raises(ValueError, match="unsafe control"):
        ActionDefinition("nav.unsafe", "Unsafe", "Navigation", f"Ctrl+K{separator}")

    bridge = UIActionBridge(actions, keymap)
    assert len(bridge.list_actions()) == len(actions.all())

    # A fresh legitimate binding still works after a denied write and restart.
    keymap.set_binding("nav.tasks", "Alt+8")
    reopened = SQLiteStore(db_path)
    reopened.initialize()
    recovered = Keymap(reopened, actions)
    assert recovered.resolve("nav.tasks") == "Alt+8"
    assert recovered.resolve("nav.agents") == initial_agents
    projected = UIActionBridge(actions, recovered).list_actions()
    tasks = next(item for item in projected if item["action_id"] == "nav.tasks")
    assert tasks["binding"] == "Alt+8"
