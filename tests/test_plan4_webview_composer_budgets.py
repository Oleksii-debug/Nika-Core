"""Plan 4 §§1–2: fail-closed WebView keymap and desktop composer boundaries."""

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


class BehavioralString(str):
    def __str__(self) -> str:
        raise AssertionError("behavioral string must not be invoked")

    def encode(self, *_args, **_kwargs):
        raise AssertionError("behavioral string must not be encoded")


class BehavioralDict(dict):
    def __init__(self) -> None:
        super().__init__(command="approved command")
        self.items_called = False

    def items(self):
        self.items_called = True
        raise AssertionError("behavioral mapping must not be iterated")


class BehavioralList(list):
    def __init__(self) -> None:
        super().__init__(["hidden"])
        self.iter_called = False

    def __iter__(self):
        self.iter_called = True
        raise AssertionError("behavioral list must not be iterated")


def _bridge(*, exported: object = '{"format_version":1,"bindings":{}}'):
    calls: list[tuple[object, ...]] = []
    keymap = SimpleNamespace(
        set_binding=lambda *args: calls.append(("set", *args)),
        restore_default=lambda *args: calls.append(("restore", *args)),
        import_json=lambda *args: calls.append(("import", *args)),
        export_json=lambda: exported,
    )
    return UIActionBridge(SimpleNamespace(), keymap), calls


@pytest.mark.parametrize(
    "bad_export",
    [BehavioralString("hidden"), 7, "", "x" * 1_048_577, "\\ud800"],
)
def test_keymap_export_refuses_unbounded_or_noncanonical_text(bad_export) -> None:
    bridge, calls = _bridge(exported=bad_export)
    result = bridge.export_keymap()
    assert result["ok"] is False
    assert "data" not in result
    assert calls == []


@pytest.mark.parametrize(
    "bad_import",
    [BehavioralString("{}"), 7, "", "x" * 1_048_577, "é" * 524_289, "\\ud800"],
)
def test_keymap_import_refuses_invalid_input_before_parser_or_effects(bad_import) -> None:
    bridge, calls = _bridge()
    assert bridge.import_keymap(bad_import)["ok"] is False
    assert calls == []


@pytest.mark.parametrize(
    "action_id,binding",
    [
        (BehavioralString("nav.tasks"), "Alt+1"),
        ("nav.tasks", BehavioralString("Alt+1")),
        ("x" * 121, "Alt+1"),
        ("nav.tasks", "x" * 257),
        ("nav.tasks", "\\ud800"),
    ],
)
def test_shortcut_mutation_refuses_invalid_or_expensive_carriers(
    action_id, binding
) -> None:
    bridge, calls = _bridge()
    assert bridge.set_binding(action_id, binding)["ok"] is False
    assert bridge.restore_default(action_id)["ok"] is (type(action_id) is str and len(action_id) <= 120)
    assert not any(call[0] == "set" for call in calls)


def test_keymap_valid_unicode_and_json_paths_remain_operational() -> None:
    data = '{"format_version":1,"bindings":{"nav.tasks":"Ctrl+1"}}'
    bridge, calls = _bridge(exported=data)
    assert bridge.export_keymap()["data"] == data
    assert bridge.import_keymap(data)["ok"] is True
    assert bridge.set_binding("nav.tasks", "Ctrl+1")["ok"] is True
    assert bridge.restore_default("nav.tasks")["ok"] is True
    assert calls == [
        ("import", data),
        ("set", "nav.tasks", "Ctrl+1"),
        ("restore", "nav.tasks"),
    ]


def _backend(tmp_path: Path, prepare):
    store = SQLiteStore(tmp_path / "nika.sqlite3")
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
    "prepared",
    [
        {"command": "approved command", "unsupported": object()},
        {"command": "approved command", "notes": "a" * 1_048_577},
        {"command": "approved command", "notes": float("nan")},
        {"command": BehavioralString("approved command")},
    ],
)
def test_composer_rejects_non_json_without_creating_tasks(
    tmp_path: Path, prepared
) -> None:
    backend, queue = _backend(tmp_path, lambda _payload: prepared)
    try:
        with pytest.raises(ValueError, match="некоректні дані"):
            backend.create_task({"command": "approved command"})
        assert queue.list_recent() == ()
        assert backend._active_futures == {}
    finally:
        backend.close()


def test_composer_rejects_behavioral_mapping_before_iteration(tmp_path: Path) -> None:
    malicious = BehavioralDict()
    backend, queue = _backend(tmp_path, lambda _payload: malicious)
    try:
        with pytest.raises(ValueError, match="JSON"):
            backend.create_task({"command": "approved command"})
        assert malicious.items_called is False
        assert queue.list_recent() == ()
    finally:
        backend.close()


def test_composer_rejects_behavioral_nested_list_before_iteration(tmp_path: Path) -> None:
    malicious = BehavioralList()
    backend, queue = _backend(
        tmp_path, lambda _payload: {"command": "approved command", "items": malicious}
    )
    try:
        with pytest.raises(ValueError, match="некоректні дані"):
            backend.create_task({"command": "approved command"})
        assert malicious.iter_called is False
        assert queue.list_recent() == ()
    finally:
        backend.close()


def test_rejected_composition_then_clean_retry_uses_detached_durable_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = {"command": "approved command", "metadata": {"stage": "initial"}}
    source = {"value": {"command": "wrong command"}}
    backend, queue = _backend(tmp_path, lambda _payload: source["value"])
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backend, "_schedule_start",
        lambda task_id, command: scheduled.append((task_id, command)),
    )
    try:
        with pytest.raises(ValueError, match="не може змінювати"):
            backend.create_task({"command": "approved command"})
        assert queue.list_recent() == ()
        source["value"] = prepared
        result = backend.create_task({"command": "approved command"})
        assert result.status == "accepted"
        assert len(scheduled) == 1
        record = queue.list_recent()[0]
        assert record.payload["metadata"] == {"stage": "initial"}
        prepared["metadata"]["stage"] = "changed after admission"
        assert queue.get(record.task_id).payload["metadata"] == {"stage": "initial"}
    finally:
        backend.close()
