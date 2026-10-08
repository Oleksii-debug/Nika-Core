"""Plan 4 Section 1: concurrent shortcut writes preserve keyboard/NVDA identity."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap
from nika_core.ui.bridge import UIActionBridge


def _setup(tmp_path: Path) -> tuple[Path, ActionRegistry, Keymap]:
    path = tmp_path / "keymap.sqlite"
    store = SQLiteStore(path)
    store.initialize()
    actions = ActionRegistry()
    actions.register(ActionDefinition("nav.tasks", "Завдання", "Навігація", "Ctrl+1"))
    actions.register(ActionDefinition("nav.agents", "Агенти", "Навігація", "Ctrl+2"))
    return path, actions, Keymap(store, actions)


def test_restore_default_rejects_taken_shortcut_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    path, actions, keymap = _setup(tmp_path)
    keymap.set_binding("nav.tasks", None)
    keymap.set_binding("nav.agents", "Ctrl+1")

    # Ctrl+1 belongs to agents now. A default restore must not silently make
    # one keystroke activate two distinct Action Registry commands.
    bridge = UIActionBridge(actions, keymap)
    denied = bridge.restore_default("nav.tasks")
    assert denied["ok"] is False
    assert "conflict" in denied["message"]
    assert keymap.resolve("nav.tasks") is None
    assert keymap.resolve("nav.agents") == "Ctrl+1"

    recovered = Keymap(SQLiteStore(path), actions)
    assert recovered.resolve("nav.tasks") is None
    assert recovered.resolve("nav.agents") == "Ctrl+1"

    with pytest.raises(ValueError, match="shortcut conflict"):
        recovered.import_json(json.dumps({
            "format_version": Keymap.FORMAT_VERSION,
            "bindings": {"nav.tasks": "Ctrl+1"},
        }))
    assert recovered.resolve("nav.tasks") is None

    recovered.set_binding("nav.agents", "Ctrl+2")
    recovered.restore_default("nav.tasks")
    again = Keymap(SQLiteStore(path), actions)
    assert again.resolve("nav.tasks") == "Ctrl+1"
    assert again.resolve("nav.agents") == "Ctrl+2"
    assert len(UIActionBridge(actions, again).list_actions()) == 2


def test_racing_keymap_instances_do_not_commit_duplicate_shortcuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, actions, first = _setup(tmp_path)
    second = Keymap(SQLiteStore(path), actions)
    first_checked = Event()
    release_first = Event()
    second_started = Event()
    original_conflict = first.conflict

    def pause_after_first_check(action_id: str, binding: str | None) -> str | None:
        result = original_conflict(action_id, binding)
        first_checked.set()
        assert release_first.wait(10), "first writer did not receive release signal"
        return result

    monkeypatch.setattr(first, "conflict", pause_after_first_check)

    def attempt(keymap: Keymap, action_id: str) -> Exception | None:
        try:
            keymap.set_binding(action_id, "Alt+9")
            return None
        except Exception as exc:  # noqa: BLE001 - assert exact transaction denial below
            return exc

    def second_write() -> Exception | None:
        second_started.set()
        return attempt(second, "nav.agents")

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer_one = executor.submit(attempt, first, "nav.tasks")
        assert first_checked.wait(10)
        writer_two = executor.submit(second_write)
        try:
            assert second_started.wait(10)
            # On the old check-then-write implementation the second writer can
            # commit while the first is suspended after observing no conflict.
            time.sleep(0.15)
        finally:
            release_first.set()
        assert writer_one.result(timeout=10) is None
        failure = writer_two.result(timeout=10)
        assert isinstance(failure, ValueError)
        assert "shortcut conflict" in str(failure)

    after_restart = Keymap(SQLiteStore(path), actions)
    assert after_restart.resolve("nav.tasks") == "Alt+9"
    assert after_restart.resolve("nav.agents") == "Ctrl+2"
    assert len(UIActionBridge(actions, after_restart).list_actions()) == 2
