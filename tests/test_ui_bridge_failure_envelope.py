from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge


class _BehavioralText(str):
    def __str__(self) -> str:
        raise AssertionError("must-not-run")


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
    assert response["message"] == (
        "Не вдалося відновити комбінацію за замовчуванням: "
        "перевірте конфлікти карти клавіш."
    )


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


@pytest.mark.parametrize("raw", [None, [], 42, "not-a-command"])
def test_dispatch_rejects_non_mapping_payload_without_transport_escape(
    tmp_path: Path, raw
) -> None:
    bridge = _bridge(tmp_path)

    response = bridge.dispatch(raw)

    assert response["request_id"] == "invalid"
    assert response["status"] == "rejected"
    assert response["message"] == "Некоректна команда інтерфейсу."


@pytest.mark.parametrize("request_id", [None, 7, "", "x" * 121])
def test_dispatch_does_not_coerce_invalid_request_id_on_rejection(
    tmp_path: Path, request_id
) -> None:
    bridge = _bridge(tmp_path)

    response = bridge.dispatch(
        {
            "request_id": request_id,
            "action_id": "INVALID ACTION",
            "payload": {},
        }
    )

    assert response["request_id"] == "invalid"
    assert response["status"] == "rejected"


def test_dispatch_preserves_canonical_request_id_on_other_validation_failure(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)

    response = bridge.dispatch(
        {
            "request_id": "request-17",
            "action_id": "INVALID ACTION",
            "payload": {},
        }
    )

    assert response["request_id"] == "request-17"
    assert response["status"] == "rejected"


@pytest.mark.parametrize(
    ("method_name", "invoke"),
    [
        ("resolve", lambda bridge: bridge.list_actions()),
        ("set_binding", lambda bridge: bridge.set_binding("nav.agents", "Alt+9")),
        ("restore_default", lambda bridge: bridge.restore_default("nav.agents")),
        ("export_json", lambda bridge: bridge.export_keymap()),
        (
            "import_json",
            lambda bridge: bridge.import_keymap('{"format_version": 1, "bindings": {}}'),
        ),
    ],
)
def test_keymap_transport_does_not_swallow_base_exception(
    tmp_path: Path, monkeypatch, method_name, invoke
) -> None:
    bridge = _bridge(tmp_path)

    def interrupt(_self: Keymap, *_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Keymap, method_name, interrupt)
    with pytest.raises(KeyboardInterrupt):
        invoke(bridge)


@pytest.mark.parametrize(
    "outcome",
    [
        object(),
        _BehavioralText("text"),
    ],
)
def test_dispatch_rejects_noncanonical_handler_result_without_coercion(
    tmp_path: Path, caplog, outcome
) -> None:
    bridge = _bridge(tmp_path, handler=lambda _payload: outcome)

    response = bridge.dispatch(
        {
            "request_id": "invalid-result-1",
            "action_id": "task.create",
            "payload": {"command": "x"},
        }
    )

    assert response == {
        "request_id": "invalid-result-1",
        "status": "failed",
        "message": "Не вдалося виконати дію через внутрішню помилку.",
        "focus_id": None,
    }
    assert "must-not-run" not in caplog.text
    assert type(outcome).__name__ in caplog.text
