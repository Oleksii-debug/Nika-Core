from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge


def _bridge(tmp_path: Path, *, handler=None, state_provider=None) -> UIActionBridge:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    handlers = {} if handler is None else {"task.create": handler}
    return UIActionBridge(
        actions,
        Keymap(store, actions),
        handlers=handlers,
        state_provider=state_provider,
    )


def test_bridge_contains_unexpected_handler_failure_without_exposing_details(
    tmp_path: Path, caplog
) -> None:
    secret_canary = "bridge-secret-canary-do-not-expose"

    def fail_unexpectedly(_payload) -> None:
        raise RuntimeError(secret_canary)

    bridge = _bridge(tmp_path, handler=fail_unexpectedly)
    result = bridge.dispatch(
        {
            "request_id": "unexpected-1",
            "action_id": "task.create",
            "payload": {"command": "x"},
        }
    )

    assert result == {
        "request_id": "unexpected-1",
        "status": "failed",
        "message": "Не вдалося виконати дію через внутрішню помилку.",
        "focus_id": None,
    }
    assert secret_canary not in result["message"]
    assert secret_canary not in caplog.text
    assert "RuntimeError" in caplog.text


def test_bridge_contains_unexpected_state_failure_without_exposing_details(
    tmp_path: Path, caplog
) -> None:
    secret_canary = "state-secret-canary-do-not-expose"

    def fail_state():
        raise OSError(secret_canary)

    bridge = _bridge(tmp_path, state_provider=fail_state)
    response = bridge.get_state()

    assert response == {
        "ok": False,
        "message": "Не вдалося отримати стан програми через внутрішню помилку.",
    }
    assert secret_canary not in response["message"]
    assert secret_canary not in caplog.text
    assert "OSError" in caplog.text


def test_restore_default_expected_conflict_returns_bounded_error(tmp_path: Path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)

    def reject_restore(_self: Keymap, _action_id: str) -> None:
        raise ValueError("shortcut conflict with nav.tasks")

    monkeypatch.setattr(Keymap, "restore_default", reject_restore)
    response = bridge.restore_default("nav.agents")

    assert response["ok"] is False
    assert response["message"] == "shortcut conflict with nav.tasks"


def test_list_actions_contains_unexpected_failure_without_exposing_details(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    secret_canary = "list-actions-secret-canary-do-not-expose"
    bridge = _bridge(tmp_path)

    def fail_resolve(_self: Keymap, _action_id: str) -> str | None:
        raise RuntimeError(secret_canary)

    monkeypatch.setattr(Keymap, "resolve", fail_resolve)

    with pytest.raises(RuntimeError) as raised:
        bridge.list_actions()

    assert str(raised.value) == "Не вдалося завантажити список дій через внутрішню помилку."
    assert secret_canary not in str(raised.value)
    assert secret_canary not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize(
    ("method_name", "invoke"),
    [
        ("set_binding", lambda bridge: bridge.set_binding("nav.agents", "Alt+9")),
        ("restore_default", lambda bridge: bridge.restore_default("nav.agents")),
        ("export_json", lambda bridge: bridge.export_keymap()),
        ("import_json", lambda bridge: bridge.import_keymap('{"schema_version": 1}')),
    ],
)
def test_keymap_transport_contains_unexpected_failures_without_exposing_details(
    tmp_path: Path, monkeypatch, caplog, method_name, invoke
) -> None:
    secret_canary = f"{method_name}-secret-canary-do-not-expose"
    bridge = _bridge(tmp_path)

    def fail_unexpectedly(_self: Keymap, *_args, **_kwargs):
        raise OSError(secret_canary)

    monkeypatch.setattr(Keymap, method_name, fail_unexpectedly)
    response = invoke(bridge)

    assert response == {
        "ok": False,
        "message": "Не вдалося змінити комбінації клавіш через внутрішню помилку.",
    }
    assert secret_canary not in response["message"]
    assert secret_canary not in caplog.text
    assert "OSError" in caplog.text
