from __future__ import annotations

from pathlib import Path

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
