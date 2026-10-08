"""Plan 4 §§1–2: screen-reader metadata and direct task-selector admission."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend


def _action_bridge(**changed):
    fields = {
        "action_id": "nav.tasks",
        "label": "Завдання",
        "category": "Навігація",
        "scope": "app",
        "may_be_unbound": True,
    }
    fields.update(changed)
    calls: list[str] = []
    keymap = SimpleNamespace(resolve=lambda _key: calls.append("resolve") or "Ctrl+1")
    bridge = UIActionBridge(
        SimpleNamespace(all=lambda: (SimpleNamespace(**fields),)),
        keymap,
    )
    return bridge, calls


@pytest.mark.parametrize(
    "changed",
    [
        {"label": "first\u2028second"},
        {"category": "first\u2029second"},
        {"may_be_unbound": 1},
        {"may_be_unbound": "false"},
        {"may_be_unbound": None},
    ],
)
def test_action_list_rejects_multiline_or_coerced_accessibility_flags(changed) -> None:
    bridge, calls = _action_bridge(**changed)
    with pytest.raises(RuntimeError, match="внутрішню помилку"):
        bridge.list_actions()
    assert calls == []


def test_action_list_preserves_plain_boolean_and_keyboard_readback() -> None:
    bridge, calls = _action_bridge(may_be_unbound=False)
    assert bridge.list_actions() == [{
        "action_id": "nav.tasks",
        "label": "Завдання",
        "category": "Навігація",
        "scope": "app",
        "binding": "Ctrl+1",
        "may_be_unbound": False,
    }]
    assert calls == ["resolve"]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_bridge_never_announces_unicode_line_separator(separator: str) -> None:
    bridge = UIActionBridge(
        SimpleNamespace(get=lambda _key: None),
        SimpleNamespace(),
        handlers={
            "nav.tasks": lambda _payload: UIResult(
                request_id="backend",
                status="completed",
                message="done" + separator + "forged",
            )
        },
    )
    response = bridge.dispatch({
        "request_id": "semantic-1", "action_id": "nav.tasks", "payload": {},
    })
    assert response["status"] == "failed"
    assert separator not in response["message"]
    assert response["request_id"] == "semantic-1"


class SpoofedControlDict(dict):
    """Behavioral carrier that would hide an explicit task selection."""

    def __init__(self) -> None:
        super().__init__(task_id="nonexistent-task")
        self.hooks = 0

    def __contains__(self, _key):
        self.hooks += 1
        return False

    def __getitem__(self, _key):
        self.hooks += 1
        raise AssertionError("behavioral selector must never be inspected")


class BehavioralTaskId(str):
    def __eq__(self, _other):
        raise AssertionError("task ID subclass comparison must not run")

    __hash__ = str.__hash__


def _backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "control.sqlite")
    store.initialize()
    queue = TaskQueue(store)
    return DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    ), queue


def test_direct_controls_reject_behavioral_and_extra_selectors_without_effects(
    tmp_path: Path,
) -> None:
    backend, queue = _backend(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "accessible control"},
    )
    queue.transition(task.task_id, TaskState.READY)
    spoofed = SpoofedControlDict()
    invalid = [
        spoofed,
        {"task_id": BehavioralTaskId(task.task_id)},
        {"task_id": object()},
        {"task_id": task.task_id, "unexpected": True},
        {"other_selector": task.task_id},
        [],
        None,
    ]
    try:
        for control in (backend.pause_task, backend.resume_task, backend.stop_agent):
            for selector in invalid:
                with pytest.raises(ValueError):
                    control(selector)
                assert queue.get(task.task_id).state == TaskState.READY
                assert backend._active_futures == {}
                assert backend._cancel_futures == {}
        assert spoofed.hooks == 0

        # Valid explicit, keyboard-selected task identity retains normal
        # pause/resume/stop behavior, without choosing a different task.
        assert backend.pause_task({"task_id": task.task_id}).status == "completed"
        assert queue.get(task.task_id).state == TaskState.PAUSED
        starts: list[tuple[str, str]] = []
        backend._schedule_start = lambda task_id, command: starts.append((task_id, command))
        assert backend.resume_task({"task_id": task.task_id}).status == "accepted"
        assert starts == [(task.task_id, "accessible control")]
        assert queue.get(task.task_id).state == TaskState.READY
        assert backend.stop_agent({"task_id": task.task_id}).status == "completed"
        assert TaskQueue(queue.store).get(task.task_id).state == TaskState.CANCELLED
    finally:
        backend.close()
