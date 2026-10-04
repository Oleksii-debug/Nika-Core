from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nika_core.config import AppConfig
from scripts import nika_windows


def _unexpected_action(_payload: Mapping[str, Any]) -> None:
    raise RuntimeError("private-action-canary")


def _unexpected_state() -> Mapping[str, Any]:
    raise OSError("private-state-canary")


def test_packaged_windows_bridge_contains_failures_and_recovers(tmp_path: Path) -> None:
    database = (tmp_path / "packaged transport.db").resolve()
    session = nika_windows.build_windows_session(AppConfig(database_path=database))
    original_handler = session.bridge._handlers["task.create"]
    original_state_provider = session.bridge._state_provider
    try:
        session.bridge._handlers["task.create"] = _unexpected_action
        result = session.bridge.dispatch(
            {
                "request_id": "transport-regression",
                "action_id": "task.create",
                "payload": {"command": "Ordinary task"},
            }
        )
        assert result["request_id"] == "transport-regression"
        assert result["status"] == "failed"
        assert "private-action-canary" not in repr(result)

        session.bridge._handlers["task.create"] = original_handler
        session.bridge._state_provider = _unexpected_state
        broken_state = session.bridge.get_state()
        assert broken_state["ok"] is False
        assert "private-state-canary" not in repr(broken_state)

        session.bridge._state_provider = original_state_provider
        healthy_state = session.bridge.get_state()
        assert healthy_state["ok"] is True
        assert isinstance(healthy_state["state"]["tasks"], list)
    finally:
        session.bridge._handlers["task.create"] = original_handler
        session.bridge._state_provider = original_state_provider
        session.close()
