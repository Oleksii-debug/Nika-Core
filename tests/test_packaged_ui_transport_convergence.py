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


class _HostileCommand(str):
    def strip(self, chars: str | None = None) -> str:
        del chars
        raise AssertionError("untrusted command methods must not execute")

    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        del sep, maxsplit
        raise AssertionError("untrusted command methods must not execute")


def test_packaged_transport_rejects_hostile_input_without_losing_durable_state(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "transport recovery українська.db").resolve()
    config = AppConfig(database_path=database)
    session = nika_windows.build_windows_session(config)
    try:
        created = session.bridge.dispatch(
            {
                "request_id": "create-product",
                "action_id": "task.create",
                "payload": {"command": "Створи застосунок для доступних нотаток"},
            }
        )
        assert created["status"] == "completed"
        before = session.bridge.get_state()
        assert before["ok"] is True
        project_id = before["state"]["product_project"]["project_id"]

        rejected = session.bridge.dispatch(
            {
                "request_id": "reject-hostile",
                "action_id": "task.create",
                "payload": {"command": _HostileCommand("Створи іншого агента")},
            }
        )
        assert rejected["request_id"] == "reject-hostile"
        assert rejected["status"] == "rejected"
        assert "untrusted command methods" not in repr(rejected)
        unchanged = session.bridge.get_state()
        assert unchanged["state"]["product_project"]["project_id"] == project_id
        assert unchanged["state"]["agent_builder_definitions"] == []
        assert unchanged["state"]["tasks"] == []

        created_agent = session.bridge.dispatch(
            {
                "request_id": "create-agent",
                "action_id": "task.create",
                "payload": {"command": "Створи агента для аналізу нотаток"},
            }
        )
        assert created_agent["status"] == "completed"
        after = session.bridge.get_state()
        assert after["state"]["product_project"]["project_id"] == project_id
        assert len(after["state"]["agent_builder_definitions"]) == 1
        agent_id = after["state"]["agent_builder_definitions"][0]["agent_id"]
        assert after["state"]["tasks"] == []
    finally:
        session.close()

    reopened = nika_windows.build_windows_session(config)
    try:
        recovered = reopened.bridge.get_state()
        assert recovered["ok"] is True
        assert recovered["state"]["product_project"]["project_id"] == project_id
        assert len(recovered["state"]["agent_builder_definitions"]) == 1
        assert recovered["state"]["agent_builder_definitions"][0]["agent_id"] == agent_id
        assert recovered["state"]["tasks"] == []
    finally:
        reopened.close()
