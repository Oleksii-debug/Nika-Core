from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.shell import index_path, launch_windows_shell


def build_bridge(tmp_path: Path) -> UIActionBridge:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    return UIActionBridge(
        actions,
        keymap,
        handlers={
            "nav.tasks": lambda _payload: "Tasks opened.",
            "task.create": lambda payload: (
                "Task accepted." if str(payload.get("command", "")).strip() else (_raise("Command is empty."))
            ),
        },
    )


def _raise(message: str) -> None:
    raise ValueError(message)


def test_bridge_rejects_unknown_action_and_unconfigured_registered_action(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    unknown = bridge.dispatch({"request_id": "1", "action_id": "shell.exec", "payload": {}})
    unavailable = bridge.dispatch({"request_id": "2", "action_id": "agent.stop", "payload": {}})
    assert unknown["status"] == "rejected"
    assert "Unknown action" in unknown["message"]
    assert unavailable["status"] == "rejected"
    assert "not available" in unavailable["message"]


def test_bridge_dispatch_and_keymap_conflict_are_fail_closed(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    accepted = bridge.dispatch(
        {"request_id": "3", "action_id": "task.create", "payload": {"command": "Research"}}
    )
    empty = bridge.dispatch(
        {"request_id": "4", "action_id": "task.create", "payload": {"command": "  "}}
    )
    conflict = bridge.set_binding("nav.agents", "Alt+1")
    assert accepted == {
        "request_id": "3",
        "status": "completed",
        "message": "Task accepted.",
        "focus_id": None,
    }
    assert empty["status"] == "rejected"
    assert conflict["ok"] is False
    assert "conflict" in conflict["message"].lower()


def test_keymap_export_import_and_clear_round_trip(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    assert bridge.set_binding("nav.workspaces", None)["ok"] is True
    exported = bridge.export_keymap()
    payload = json.loads(exported["data"])
    assert payload["bindings"]["nav.workspaces"] is None
    payload["bindings"]["nav.workspaces"] = "Alt+4"
    imported = bridge.import_keymap(json.dumps(payload))
    assert imported["ok"] is True
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["nav.workspaces"]["binding"] == "Alt+4"
    assert bridge.import_keymap("not-json")["ok"] is False


def test_list_actions_exposes_resolved_bindings_without_handlers(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["task.create"]["binding"] == "Ctrl+N"
    assert actions["task.create"]["may_be_unbound"] is False
    assert actions["nav.workspaces"]["binding"] == "Alt+4"
    assert "handler" not in actions["task.create"]


def test_local_html_has_required_semantics_and_registered_action_ids(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    html = index_path().read_text(encoding="utf-8")
    assert '<html lang="uk">' in html
    assert '<main id="main" tabindex="-1">' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert '<label for="command-input">' in html
    assert '<label for="keymap-json">' in html
    assert '<caption>Комбінації клавіш Nika Core</caption>' in html
    assert 'id="workspaces-heading"' in html
    registered = {item["action_id"] for item in bridge.list_actions()}
    for action_id in (
        "nav.tasks",
        "nav.agents",
        "nav.logs",
        "nav.workspaces",
        "task.create",
        "task.pause",
        "task.resume",
        "agent.stop",
    ):
        assert action_id in registered
        assert f'data-action-id="{action_id}"' in html


def test_shell_forces_edgechromium_and_supported_local_path(monkeypatch, tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    calls: dict[str, object] = {}
    fake_window = object()

    def create_window(title: str, url: str, **kwargs):
        calls["title"] = title
        calls["url"] = url
        calls["kwargs"] = kwargs
        return fake_window

    def start(**kwargs):
        calls["start"] = kwargs

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    window = launch_windows_shell(bridge)
    assert window is fake_window
    assert calls["title"] == "Nika Core"
    local_url = str(calls["url"])
    assert not local_url.startswith("file:")
    assert Path(local_url).is_absolute()
    assert Path(local_url).name == "index.html"
    assert calls["start"] == {"gui": "edgechromium"}
    assert calls["kwargs"]["js_api"] is bridge


def test_javascript_preserves_edit_shortcuts_and_wires_keymap_transfer() -> None:
    script = index_path().with_name("app.js").read_text(encoding="utf-8")
    assert 'new Set(["a", "c", "x", "v", "z", "y"])' in script
    assert 'window.addEventListener("pywebviewready"' in script
    assert "if (globalThis.pywebview?.api)" in script
    assert "async function initializeBridge()" in script
    assert "await refreshKeymap()" in script
    assert 'dataset.nikaReady = "true"' in script
    assert "if (!actionsReady) return;" in script
    assert "focusElementById(focusId)" in script
    assert "event.preventDefault()" in script
    assert "globalThis.pywebview.api.set_binding" in script
    assert "globalThis.pywebview.api.export_keymap" in script
    assert "globalThis.pywebview.api.import_keymap" in script


def test_packaged_uia_gate_waits_for_bridge_readiness_before_hotkeys() -> None:
    proof = Path(__file__).parents[1] / "scripts" / "m5_uia_proof.ps1"
    script = proof.read_text(encoding="utf-8")
    ready_wait = script.index("Wait-DescendantName 'Nika Core готова до роботи.'")
    alt_hotkey = script.index("SendWait('%1')")
    command_hotkey = script.index("SendWait('^+p')")
    assert ready_wait < alt_hotkey < command_hotkey
    assert "keyboard/focus flow verified successfully" in script
