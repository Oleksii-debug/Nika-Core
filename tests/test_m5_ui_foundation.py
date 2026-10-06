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
    assert unknown == {
        "request_id": "1",
        "status": "rejected",
        "message": "Невідома дія інтерфейсу.",
        "focus_id": None,
    }
    assert unavailable == {
        "request_id": "2",
        "status": "rejected",
        "message": "Ця дія недоступна в поточному контексті.",
        "focus_id": None,
    }


def test_bridge_dispatch_and_keymap_conflict_are_fail_closed(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    accepted = bridge.dispatch(
        {"request_id": "3", "action_id": "task.create", "payload": {"command": "Research"}}
    )
    empty = bridge.dispatch(
        {"request_id": "4", "action_id": "task.create", "payload": {"command": "  "}}
    )
    conflict = bridge.set_binding("nav.agents", "Alt+1")
    saved = bridge.set_binding("nav.agents", "Alt+5")
    assert accepted == {
        "request_id": "3",
        "status": "completed",
        "message": "Task accepted.",
        "focus_id": None,
    }
    assert empty["status"] == "rejected"
    assert conflict == {
        "ok": False,
        "message": (
            "Не вдалося зберегти комбінацію: "
            "перевірте дію, формат і конфлікти."
        ),
    }
    assert saved == {"ok": True, "message": "Комбінацію клавіш збережено."}

def test_keymap_export_import_and_clear_round_trip(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    assert bridge.set_binding("nav.workspaces", None)["ok"] is True
    exported = bridge.export_keymap()
    assert exported["message"] == "Карту клавіш експортовано."
    payload = json.loads(exported["data"])
    assert payload["bindings"]["nav.workspaces"] is None
    payload["bindings"]["nav.workspaces"] = "Alt+4"
    imported = bridge.import_keymap(json.dumps(payload))
    assert imported == {"ok": True, "message": "Карту клавіш імпортовано."}
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["nav.workspaces"]["binding"] == "Alt+4"
    assert bridge.import_keymap("not-json") == {
        "ok": False,
        "message": (
            "Не вдалося імпортувати карту клавіш: "
            "перевірте JSON, дії та конфлікти."
        ),
    }
    assert bridge.import_keymap(None) == {
        "ok": False,
        "message": "Карта клавіш має бути текстом JSON.",
    }
    assert bridge.restore_default("nav.workspaces") == {
        "ok": True,
        "message": "Комбінацію за замовчуванням відновлено.",
    }
    assert bridge.restore_default("missing.action") == {
        "ok": False,
        "message": (
            "Не вдалося відновити комбінацію за замовчуванням: "
            "невідома дія."
        ),
    }

def test_bridge_owned_failures_match_ukrainian_shell_language(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)

    invalid = bridge.dispatch({"request_id": "invalid"})
    assert invalid["status"] == "rejected"
    assert invalid["message"] == "Некоректна команда інтерфейсу."

    unknown = bridge.dispatch(
        {"request_id": "unknown", "action_id": "shell.exec", "payload": {}}
    )
    assert unknown["status"] == "rejected"
    assert unknown["message"] == "Невідома дія інтерфейсу."

    unavailable = bridge.dispatch(
        {"request_id": "unavailable", "action_id": "agent.stop", "payload": {}}
    )
    assert unavailable["status"] == "rejected"
    assert unavailable["message"] == "Ця дія недоступна в поточному контексті."

    assert bridge.get_state() == {
        "ok": False,
        "message": "Джерело стану програми недоступне.",
    }


def test_keymap_known_failures_remain_localized_and_serializable(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = build_bridge(tmp_path)

    monkeypatch.setattr(
        bridge._keymap,
        "restore_default",
        lambda _action_id: _raise("shortcut conflict with nav.tasks"),
    )
    assert bridge.restore_default("nav.workspaces") == {
        "ok": False,
        "message": (
            "Не вдалося відновити комбінацію за замовчуванням: "
            "перевірте конфлікти карти клавіш."
        ),
    }

    monkeypatch.setattr(
        bridge._keymap,
        "export_json",
        lambda: _raise("stored keymap binding must be text"),
    )
    assert bridge.export_keymap() == {
        "ok": False,
        "message": (
            "Не вдалося експортувати карту клавіш: "
            "перевірте збережені налаштування."
        ),
    }


def test_strict_keymap_rejections_remain_localized_and_atomic(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)

    assert bridge.set_binding("nav.agents", "Alt+5")["ok"] is True
    assert bridge.set_binding("nav.tasks", "Alt+2")["ok"] is True

    conflict = bridge.restore_default("nav.agents")
    assert conflict == {
        "ok": False,
        "message": (
            "Не вдалося відновити комбінацію за замовчуванням: "
            "перевірте конфлікти карти клавіш."
        ),
    }
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["nav.agents"]["binding"] == "Alt+5"
    assert actions["nav.tasks"]["binding"] == "Alt+2"

    control_laden = bridge.set_binding("nav.logs", "Ctrl+\nK")
    assert control_laden == {
        "ok": False,
        "message": (
            "Не вдалося зберегти комбінацію: "
            "перевірте дію, формат і конфлікти."
        ),
    }

    duplicate = bridge.import_keymap(
        '{"format_version":1,"bindings":{"nav.logs":"Alt+8","nav.logs":"Alt+9"}}'
    )
    assert duplicate == {
        "ok": False,
        "message": (
            "Не вдалося імпортувати карту клавіш: "
            "перевірте JSON, дії та конфлікти."
        ),
    }
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["nav.logs"]["binding"] == "Alt+3"
    assert actions["nav.agents"]["binding"] == "Alt+5"
    assert actions["nav.tasks"]["binding"] == "Alt+2"


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
    assert (
        'aria-describedby="execution-mode command-intelligence-help task-control-help '
        'product-decision-help product-factory-help"'
        in html
    )
    assert 'id="product-factory-help"' in html
    assert "покажи поточний статус Product Factory" in html
    assert '<label for="recovery-approval-task">' in html
    assert 'id="recovery-approval-task"' in html
    assert 'data-action-id="recovery.approve"' in html
    assert 'data-action-id="recovery.reject"' in html
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
        "recovery.approve",
        "recovery.reject",
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
    keydown = script.index('document.addEventListener("keydown"')
    editable_guard = script.index("if (isEditable(event.target)) return;", keydown)
    prevent_default = script.index("event.preventDefault()", keydown)
    assert editable_guard < prevent_default
    assert 'target.matches("input, textarea, select")' in script
    assert "target.isContentEditable" in script
    assert 'new Set(["a", "c", "x", "v", "z", "y"])' not in script
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
    assert "snapshot.approval_task_ids.length > 8" in script
    assert 'option.textContent = `Завдання ${taskId}`' in script
    assert "if (recoveryApprovalAction) payload.task_id = recoveryApprovalTask.value;" in script


def test_packaged_uia_gate_waits_for_bridge_readiness_before_hotkeys() -> None:
    proof = Path(__file__).parents[1] / "scripts" / "m5_uia_proof.ps1"
    script = proof.read_text(encoding="utf-8")
    ready_wait = script.index("Wait-BoundTextEvidence 'Nika Core готова до роботи.'")
    alt_hotkey = script.index("SendWait('%1')")
    command_hotkey = script.index("SendWait('^+p')")
    assert ready_wait < alt_hotkey < command_hotkey
    assert "keyboard/focus flow verified successfully" in script


def test_packaged_uia_source_status_oracle_matches_production_contract() -> None:
    root = Path(__file__).parents[1]
    proof = (root / "scripts" / "m5_uia_proof.ps1").read_text(encoding="utf-8")
    source_settings = (root / "src" / "nika_core" / "v01_source_settings.py").read_text(
        encoding="utf-8"
    )
    status = "Джерела збережено для нових завдань. Можна створити командне завдання."
    assert status in source_settings
    assert f"Wait-BoundTextEvidence '{status}'" in proof
    assert "Джерела збережено. Можна створити нове командне завдання." not in proof
