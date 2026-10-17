"""Plan 4 §§1–2: accessible action projection and direct task ingress safety."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


class HostileDict(dict):
    def __init__(self) -> None:
        super().__init__(command="permitted")
        self.visits = 0

    def get(self, *args, **kwargs):
        self.visits += 1
        raise AssertionError("behavioral dict lookup must not run")

    def items(self):
        self.visits += 1
        raise AssertionError("behavioral dict traversal must not run")


class HostileString(str):
    def encode(self, *_args, **_kwargs):
        raise AssertionError("behavioral string encoding must not run")


def _action_bridge(**overrides):
    action = {
        "action_id": "nav.tasks",
        "label": "Завдання",
        "category": "Навігація",
        "scope": "app",
        "may_be_unbound": True,
    }
    action.update(overrides)
    definition = SimpleNamespace(**action)
    keymap = SimpleNamespace(resolve=lambda _action_id: "Ctrl+1")
    bridge = UIActionBridge(
        SimpleNamespace(all=lambda: (definition,)),
        keymap,
    )
    return bridge


@pytest.mark.parametrize(
    "changed",
    [
        {"action_id": "nav.tasks\\nspoof".replace("\\n", "\n")},
        {"action_id": HostileString("nav.tasks")},
        {"label": "ready\\nspoof".replace("\\n", "\n")},
        {"label": "direction-\u202e"},
        {"label": "x" * 2049},
        {"category": "x\x00y"},
        {"scope": "a\ud800b"},
    ],
)
def test_action_list_refuses_unsafe_assistive_metadata(changed) -> None:
    bridge = _action_bridge(**changed)
    with pytest.raises(RuntimeError, match="внутрішню помилку") as error:
        bridge.list_actions()
    assert "spoof" not in str(error.value)


@pytest.mark.parametrize(
    "binding",
    [
        "Ctrl+1\\nspoof".replace("\\n", "\n"),
        "unicode-\u202e",
        "é" * 129,
        HostileString("Ctrl+1"),
        {"shortcut": "Ctrl+1"},
    ],
)
def test_action_list_refuses_unsafe_keymap_readback(binding) -> None:
    bridge = _action_bridge()
    bridge._keymap = SimpleNamespace(resolve=lambda _action_id: binding)
    with pytest.raises(RuntimeError, match="внутрішню помилку"):
        bridge.list_actions()


def test_action_list_keeps_clean_keyboard_metadata() -> None:
    bridge = _action_bridge()
    items = bridge.list_actions()
    assert items == [{
        "action_id": "nav.tasks",
        "label": "Завдання",
        "category": "Навігація",
        "scope": "app",
        "binding": "Ctrl+1",
        "may_be_unbound": True,
    }]


def _backend(tmp_path: Path, *, prepare=None):
    store = SQLiteStore(tmp_path / "plan4.sqlite")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        prepare_task_payload=prepare,
    )
    return backend, queue


@pytest.mark.parametrize(
    "bad",
    [
        HostileDict(),
        {"command": HostileString("permitted")},
        {"command": "permitted", "extra": {"array": float("nan")}},
        {"command": "permitted", "extra": object()},
        {"command": "permitted", "extra": "x" * 1_048_577},
    ],
)
def test_direct_task_creation_refuses_hostile_payload_without_effects(
    tmp_path: Path, bad
) -> None:
    composer_calls = []
    backend, queue = _backend(tmp_path, prepare=lambda payload: composer_calls.append(payload))
    try:
        with pytest.raises(ValueError):
            backend.create_task(bad)
        assert composer_calls == []
        assert queue.list_recent() == ()
        assert backend._active_futures == {}
        if isinstance(bad, HostileDict):
            assert bad.visits == 0
    finally:
        backend.close()


def test_direct_task_creation_detaches_nested_input_and_allows_clean_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, queue = _backend(tmp_path)
    scheduled = []
    monkeypatch.setattr(
        backend,
        "_schedule_start",
        lambda task_id, command: scheduled.append((task_id, command)),
    )
    try:
        with pytest.raises(ValueError):
            backend.create_task({"command": "permitted", "payload": float("inf")})
        assert queue.list_recent() == ()
        result = backend.create_task({"command": "permitted"})
        assert result.status == "accepted"
        record = queue.list_recent()[0]
        assert record.payload == {"command": "permitted"}
        assert scheduled == [(record.task_id, "permitted")]
    finally:
        backend.close()
