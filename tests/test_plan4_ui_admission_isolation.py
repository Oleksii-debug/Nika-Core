"""Plan 4 S1/S2: reject behavioral UI carriers and cross-workspace task probes."""

from __future__ import annotations

from collections.abc import Mapping
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
from nika_core.ui.desktop_backend import DesktopBackend


def _backend(tmp_path: Path, *, prepare=None) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "nika.db")
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


class HostileMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.calls = 0

    def __getitem__(self, key: str) -> object:
        self.calls += 1
        raise AssertionError("untrusted mapping read")

    def __iter__(self):
        self.calls += 1
        raise AssertionError("untrusted mapping iteration")

    def __len__(self) -> int:
        self.calls += 1
        raise AssertionError("untrusted mapping length")


class HostileDict(dict):
    def __init__(self) -> None:
        super().__init__(request_id="hostile", action_id="task.create", payload={})
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("subclass mapping lookup")

    def items(self):
        self.calls += 1
        raise AssertionError("subclass mapping items")


@pytest.mark.parametrize("make_carrier", [HostileMapping, HostileDict])
def test_ui_bridge_rejects_behavioral_top_level_carriers_before_introspection(
    make_carrier,
) -> None:
    raw = make_carrier()
    calls: list[object] = []
    bridge = UIActionBridge(
        SimpleNamespace(get=lambda _action_id: calls.append("registry")),
        SimpleNamespace(),
        handlers={"task.create": lambda _payload: calls.append("effect")},
    )
    assert bridge.dispatch(raw) == {
        "request_id": "invalid",
        "status": "rejected",
        "message": "Invalid UI command: expected a plain JSON object.",
        "focus_id": None,
    }
    assert raw.calls == 0
    assert calls == []


def test_ui_bridge_keeps_plain_json_action_flow() -> None:
    calls: list[dict[str, object]] = []
    bridge = UIActionBridge(
        SimpleNamespace(get=lambda _action_id: None),
        SimpleNamespace(),
        handlers={"task.create": lambda payload: calls.append(dict(payload)) or "accepted"},
    )
    result = bridge.dispatch(
        {"request_id": "r1", "action_id": "task.create", "payload": {"command": "safe"}}
    )
    assert result["status"] == "completed"
    assert result["request_id"] == "r1"
    assert calls == [{"command": "safe"}]


class ImpersonatedCommand:
    def __init__(self) -> None:
        self.comparisons = 0

    def __eq__(self, other: object) -> bool:
        self.comparisons += 1
        return True

    def __str__(self) -> str:
        raise AssertionError("untrusted command must not stringify")


def test_prepared_command_cannot_impersonate_valid_text(tmp_path: Path) -> None:
    injected = ImpersonatedCommand()
    backend, queue = _backend(tmp_path, prepare=lambda _payload: {"command": injected})
    try:
        with pytest.raises(ValueError, match="не може змінювати"):
            backend.create_task({"command": "approved text"})
        assert injected.comparisons == 0
        assert queue.list_recent() == ()
    finally:
        backend.close()


def test_prepared_string_subclass_is_not_authoritative(tmp_path: Path) -> None:
    class SubclassedCommand(str):
        def __eq__(self, other: object) -> bool:
            raise AssertionError("subclass equality invoked")

        __hash__ = str.__hash__

    backend, queue = _backend(
        tmp_path, prepare=lambda _payload: {"command": SubclassedCommand("approved text")}
    )
    try:
        with pytest.raises(ValueError, match="не може змінювати"):
            backend.create_task({"command": "approved text"})
        assert queue.list_recent() == ()
    finally:
        backend.close()


def test_foreign_task_and_absent_id_are_indistinguishable(tmp_path: Path) -> None:
    backend, queue = _backend(tmp_path)
    foreign = queue.create(
        workspace_id="other-workspace",
        agent_id="other-agent",
        payload={"command": "private"},
    )
    queue.transition(foreign.task_id, TaskState.READY)
    try:
        for control in (backend.pause_task, backend.resume_task, backend.stop_agent):
            errors = []
            for task_id in (foreign.task_id, "absent-task-id"):
                with pytest.raises(ValueError) as caught:
                    control({"task_id": task_id})
                errors.append(str(caught.value))
            assert errors == ["Вказане завдання не знайдено."] * 2
            assert queue.get(foreign.task_id).state == TaskState.READY
        assert backend._active_futures == {}
        assert backend._cancel_futures == {}
    finally:
        backend.close()


@pytest.mark.parametrize(
    "bad_id",
    [
        "forged\nINFO: accepted",
        "unsafe\x00tail",
        "direction-\u202e",
        "contains whitespace",
        "nonascii-\u00e9",
        "x" * 121,
    ],
)
def test_ui_bridge_rejects_status_spoofing_request_ids_before_effects(bad_id: str) -> None:
    calls: list[str] = []
    bridge = UIActionBridge(
        SimpleNamespace(get=lambda action_id: calls.append(action_id)),
        SimpleNamespace(),
        handlers={"task.create": lambda _payload: calls.append("effect")},
    )
    result = bridge.dispatch({
        "request_id": bad_id,
        "action_id": "task.create",
        "payload": {"command": "safe"},
    })
    assert result["status"] == "rejected"
    assert result["request_id"] == "invalid"
    assert bad_id not in result["message"]
    assert calls == []


def test_ui_bridge_rejects_request_id_subclass_without_behavioral_calls() -> None:
    class BehavioralId(str):
        def __str__(self) -> str:
            raise AssertionError("untrusted request ID stringification")

    bridge = UIActionBridge(SimpleNamespace(), SimpleNamespace())
    response = bridge.dispatch({
        "request_id": BehavioralId("normal"),
        "action_id": "task.create",
        "payload": {"command": "safe"},
    })
    assert response["status"] == "rejected"
    assert response["request_id"] == "invalid"


def test_desktop_task_projection_excludes_foreign_scope_even_after_restart(
    tmp_path: Path,
) -> None:
    backend, queue = _backend(tmp_path)
    own = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "public task"},
    )
    foreign = [
        queue.create(
            workspace_id="private-workspace",
            agent_id="private-agent",
            payload={"command": f"PRIVATE TASK {i}"},
        )
        for i in range(60)
    ]
    other_agent = queue.create(
        workspace_id="default",
        agent_id="private-agent",
        payload={"command": "PRIVATE AGENT TASK"},
    )
    try:
        tasks = backend.snapshot()["tasks"]
        assert [item["task_id"] for item in tasks] == [own.task_id]
        assert all(item["command"] == "public task" for item in tasks)
        bridge = UIActionBridge(
            SimpleNamespace(), SimpleNamespace(), state_provider=backend.snapshot
        )
        view = bridge.get_state()
        assert view["ok"] is True
        assert view["state"]["tasks"] == tasks
        assert all(
            item["task_id"] not in {record.task_id for record in foreign}
            for item in view["state"]["tasks"]
        )
        assert other_agent.task_id not in {item["task_id"] for item in tasks}
    finally:
        backend.close()

    restarted, _ = _backend(tmp_path)
    try:
        assert [item["task_id"] for item in restarted.snapshot()["tasks"]] == [
            own.task_id
        ]
    finally:
        restarted.close()


def test_keymap_webview_boundary_rejects_behavioral_strings_without_calls() -> None:
    calls: list[tuple[object, ...]] = []

    class BehavioralText(str):
        def __hash__(self) -> int:
            raise AssertionError("behavioral action ID hash")

        def __str__(self) -> str:
            raise AssertionError("behavioral keymap input stringification")

    keymap = SimpleNamespace(
        set_binding=lambda *args: calls.append(("set", *args)),
        restore_default=lambda *args: calls.append(("restore", *args)),
        import_json=lambda *args: calls.append(("import", *args)),
    )
    bridge = UIActionBridge(SimpleNamespace(), keymap)
    hostile = BehavioralText("nav.tasks")
    assert bridge.set_binding(hostile, "Alt+1")["ok"] is False
    assert bridge.set_binding("nav.tasks", hostile)["ok"] is False
    assert bridge.restore_default(hostile)["ok"] is False
    assert bridge.import_keymap(BehavioralText("{}"))["ok"] is False
    assert calls == []

    assert bridge.set_binding("nav.tasks", "Alt+1")["ok"] is True
    assert bridge.restore_default("nav.tasks")["ok"] is True
    assert bridge.import_keymap("{}")["ok"] is True
    assert calls == [
        ("set", "nav.tasks", "Alt+1"),
        ("restore", "nav.tasks"),
        ("import", "{}"),
    ]


def test_task_acceptance_does_not_announce_private_command(tmp_path: Path) -> None:
    backend, queue = _backend(tmp_path)
    sensitive = "token-canary-private-do-not-announce"
    try:
        result = backend.create_task({"command": sensitive})
        assert result.status == "accepted"
        assert sensitive not in result.message
        assert result.message == "Завдання прийнято до виконання."
        # The authorized task authority still keeps the original command.
        assert queue.list_recent()[0].payload["command"] == sensitive
    finally:
        backend.close()


def test_desktop_projection_skips_task_disappearing_during_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, queue = _backend(tmp_path)
    vanished = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "vanished"},
    )
    available = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "available"},
    )
    actual_get = queue.get

    def get_with_concurrent_removal(task_id: str):
        if task_id == vanished.task_id:
            raise KeyError(task_id)
        return actual_get(task_id)

    monkeypatch.setattr(queue, "get", get_with_concurrent_removal)
    try:
        tasks = backend.snapshot()["tasks"]
        assert [task["task_id"] for task in tasks] == [available.task_id]
        bridge = UIActionBridge(
            SimpleNamespace(), SimpleNamespace(), state_provider=backend.snapshot
        )
        response = bridge.get_state()
        assert response["ok"] is True
        assert response["state"]["tasks"] == tasks
    finally:
        backend.close()
