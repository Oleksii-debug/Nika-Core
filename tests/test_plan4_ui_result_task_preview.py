"""Plan 4 §§1–2: exact UI result projection and bounded NVDA task previews."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika_core.kernel.task_state import TaskState
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend


def _dispatch(result: UIResult) -> dict[str, object]:
    bridge = UIActionBridge(
        SimpleNamespace(get=lambda _action: object()),
        SimpleNamespace(),
        handlers={"nav.tasks": lambda _payload: result},
    )
    return bridge.dispatch({
        "request_id": "screenreader-1",
        "action_id": "nav.tasks",
        "payload": {},
    })


class BehavioralRequestId(str):
    def __ne__(self, _other: object) -> bool:
        raise AssertionError("handler request IDs must not be compared")


def test_result_rebuilds_safe_identity_without_evaluating_hostile_request_id() -> None:
    result = UIResult(
        request_id="handler",
        status="completed",
        message="Завершено",
        focus_id="tasks-heading",
    )
    object.__setattr__(result, "request_id", BehavioralRequestId("handler"))
    assert _dispatch(result) == {
        "request_id": "screenreader-1",
        "status": "completed",
        "message": "Завершено",
        "focus_id": "tasks-heading",
    }


@pytest.mark.parametrize("spoofed", ["completed\u2028failed", 1, True, object()])
def test_mutated_frozen_result_status_fails_closed(spoofed: object) -> None:
    result = UIResult(request_id="handler", status="completed", message="Безпечний стан")
    object.__setattr__(result, "status", spoofed)
    response = _dispatch(result)
    assert response["request_id"] == "screenreader-1"
    assert response["status"] == "failed"
    assert "Безпечний стан" not in response["message"]


class BehavioralOutcome(UIResult):
    def __getattribute__(self, key: str):
        if key in {"status", "message", "focus_id", "request_id"}:
            raise AssertionError("behavioral result was inspected")
        return super().__getattribute__(key)


def test_behavioral_result_subclass_cannot_execute_getters_at_bridge() -> None:
    result = BehavioralOutcome(
        request_id="handler", status="completed", message="untrusted"
    )
    response = _dispatch(result)
    assert response["status"] == "failed"
    assert response["request_id"] == "screenreader-1"


def _record(command: object) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-1",
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
        state=TaskState.READY,
    )


def test_accessible_task_preview_is_bounded_and_preserves_durable_payload() -> None:
    original = "Перевірити " + "X" * 2500
    record = _record(original)
    view = DesktopBackend._task_view(record)
    assert view["command"] == original[:160] + "…"
    assert len(view["command"]) == 161
    assert record.payload["command"] == original
    assert view["task_id"] == "task-1"


def test_accessible_task_preview_removes_control_bidi_and_unicode_separators() -> None:
    result = DesktopBackend._task_view(
        _record("Перше\u202eдруге\nтретє\u2028четверте\u2029п'яте\ud800")
    )
    preview = result["command"]
    assert "Перше" in preview and "п'яте" in preview
    for bad in ("\u202e", "\n", "\u2028", "\u2029", "\ud800"):
        assert bad not in preview


def test_task_preview_rejects_behavioral_command_carrier_without_str() -> None:
    class Poisoned:
        def __str__(self):
            raise AssertionError("must never stringify command")

    assert DesktopBackend._task_view(_record(Poisoned()))["command"] == ""
    assert DesktopBackend._task_view(_record("Зробити звіт"))["command"] == "Зробити звіт"
