from __future__ import annotations


def test_real_bridge_rejects_bad_payload_without_invoking_handler() -> None:
    from types import SimpleNamespace

    from nika_core.ui.bridge import UIActionBridge

    invoked: list[object] = []
    actions = SimpleNamespace(get=lambda _action_id: object())
    bridge = UIActionBridge(
        actions,
        SimpleNamespace(),
        handlers={"task.create": lambda payload: invoked.append(payload) or "accepted"},
    )
    for payload in ({"command": "\ud800"}, {"items": [None] * 9_998}, {"bad": float("nan")}):
        result = bridge.dispatch(
            {"request_id": "r1", "action_id": "task.create", "payload": payload}
        )
        assert result["request_id"] == "r1"
        assert result["status"] == "rejected"
        assert result["message"].startswith("Invalid UI command:")
        assert invoked == []

    accepted = bridge.dispatch(
        {"request_id": "r2", "action_id": "task.create", "payload": {"command": "Тест"}}
    )
    assert accepted["status"] == "completed"
    assert invoked == [{"command": "Тест"}]
