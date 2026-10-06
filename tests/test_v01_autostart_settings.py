from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.ui.autostart_settings import AutostartSettings
from nika_core.windows_autostart import WindowsAutostartService
from scripts import nika_windows


class Registration:
    def __init__(self) -> None:
        self.value: str | None = None
        self.writes: list[str] = []
        self.read_error = False
        self.write_error = False
        self.ignore_write = False
        self.ignore_delete = False

    def read(self) -> str | None:
        if self.read_error:
            raise OSError("PRIVATE_REGISTRY_CANARY")
        return self.value

    def write(self, command: str) -> None:
        if self.write_error:
            raise OSError("PRIVATE_REGISTRY_CANARY")
        self.writes.append(command)
        if not self.ignore_write:
            self.value = command

    def delete(self) -> None:
        if not self.ignore_delete:
            self.value = None


def settings(tmp_path: Path):
    store = SQLiteStore(tmp_path / "settings.db")
    store.initialize()
    registration = Registration()
    service = WindowsAutostartService(Path(r"C:\Олексій\Nika Core\Nika.exe"), registration)
    return AutostartSettings(service, AuditLog(store)), registration, store, service


def test_setting_reads_never_write_and_recreated_adapter_reads_os_truth(tmp_path: Path) -> None:
    adapter, registration, store, service = settings(tmp_path)
    assert adapter.snapshot()["state"] == "disabled"
    assert adapter.refresh({}).status == "completed"
    assert registration.writes == []
    assert adapter.configure({"enabled": True}).status == "completed"
    assert adapter.configure({"enabled": True}).status == "completed"
    assert registration.writes == [service.expected_command]
    restarted = AutostartSettings(
        WindowsAutostartService(Path(r"C:\Олексій\Nika Core\Nika.exe"), registration),
        AuditLog(store),
    )
    assert restarted.snapshot()["state"] == "enabled"
    assert restarted.configure({"enabled": False}).status == "completed"
    assert adapter.snapshot()["state"] == "disabled"
    assert TaskQueue(store).list_recent() == ()
    events = AuditLog(store).list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )
    assert [event.event_type.rsplit(".", 1)[-1] for event in events] == [
        "requested",
        "confirmed",
    ] * 3
    assert all(set(event.payload) == {"enabled"} for event in events)
    assert "Олексій" not in json.dumps(
        [adapter.snapshot(), [e.payload for e in events]], ensure_ascii=False
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabled": 1},
        {"enabled": "true"},
        {"enabled": None},
        {"enabled": []},
        {"enabled": False, "command": "PRIVATE_REGISTRY_CANARY"},
        {"path": "C:\\untrusted.exe"},
    ],
)
def test_only_explicit_boolean_intent_crosses_setting_boundary(tmp_path: Path, payload) -> None:
    adapter, registration, store, _ = settings(tmp_path)
    result = adapter.configure(payload)
    assert result.status == "rejected"
    assert registration.writes == []
    assert "PRIVATE_REGISTRY_CANARY" not in result.model_dump_json()
    assert (
        AuditLog(store).list_for(entity_type="application_setting", entity_id="windows.autostart")
        == ()
    )


def test_foreign_or_moved_registration_is_stale_until_explicit_choice(tmp_path: Path) -> None:
    adapter, registration, _, service = settings(tmp_path)
    registration.value = '"C:\\PRIVATE_REGISTRY_CANARY\\Old.exe"'
    state = adapter.snapshot()
    assert state["state"] == "stale" and state["can_change"] is True
    assert "PRIVATE_REGISTRY_CANARY" not in json.dumps(state)
    assert registration.writes == []
    assert adapter.configure({"enabled": True}).status == "completed"
    assert registration.value == service.expected_command
    registration.value = "PRIVATE_REGISTRY_CANARY"
    assert adapter.configure({"enabled": False}).status == "completed"
    assert registration.value is None


@pytest.mark.parametrize("fault", ["read_error", "write_error", "ignore_write"])
def test_failed_read_or_write_never_falsely_confirms_or_exposes_os_error(
    tmp_path: Path, fault: str
) -> None:
    adapter, registration, _, _ = settings(tmp_path)
    setattr(registration, fault, True)
    result = adapter.configure({"enabled": True})
    assert result.status == "failed"
    assert "PRIVATE_REGISTRY_CANARY" not in result.model_dump_json()
    state = adapter.snapshot()
    assert state["state"] == ("error" if fault == "read_error" else "disabled")
    assert "PRIVATE_REGISTRY_CANARY" not in json.dumps(state)


def test_failed_disable_reports_failure_and_preserves_actual_enabled_truth(tmp_path: Path) -> None:
    adapter, registration, _, service = settings(tmp_path)
    registration.value = service.expected_command
    registration.ignore_delete = True
    assert adapter.configure({"enabled": False}).status == "failed"
    assert adapter.snapshot()["state"] == "enabled"


def test_audit_failure_before_write_prevents_os_mutation(tmp_path: Path, monkeypatch) -> None:
    adapter, registration, _, _ = settings(tmp_path)

    def fail(**_kwargs):
        raise OSError("PRIVATE_REGISTRY_CANARY")

    monkeypatch.setattr(adapter._audit, "append", fail)
    assert adapter.configure({"enabled": True}).status == "failed"
    assert registration.writes == []


def test_audit_failure_after_write_does_not_hide_real_registration(
    tmp_path: Path, monkeypatch
) -> None:
    adapter, registration, _, service = settings(tmp_path)
    append = adapter._audit.append

    def fail_confirmed(**kwargs):
        if kwargs["event_type"].endswith("confirmed"):
            raise OSError("PRIVATE_REGISTRY_CANARY")
        return append(**kwargs)

    monkeypatch.setattr(adapter._audit, "append", fail_confirmed)
    assert adapter.configure({"enabled": True}).status == "failed"
    assert registration.value == service.expected_command
    assert adapter.snapshot()["state"] == "enabled"


def test_concurrent_same_intent_is_serialized_and_idempotent(tmp_path: Path) -> None:
    adapter, registration, _, _ = settings(tmp_path)
    barrier = Barrier(2)

    def enable():
        barrier.wait(timeout=5)
        return adapter.configure({"enabled": True}).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(enable) for _ in range(2)]
        assert [job.result(timeout=10) for job in jobs] == ["completed", "completed"]
    assert len(registration.writes) == 1


@pytest.mark.parametrize("platform,frozen", [("linux", True), ("win32", False), ("linux", False)])
def test_source_or_non_windows_build_never_registers_python(
    tmp_path: Path, monkeypatch, platform, frozen
) -> None:
    monkeypatch.setattr(
        nika_windows,
        "sys",
        SimpleNamespace(platform=platform, frozen=frozen, executable="PRIVATE_REGISTRY_CANARY"),
    )

    def forbidden(*_args):
        raise AssertionError("Must not construct an OS registration service")

    monkeypatch.setattr(nika_windows, "WindowsAutostartService", forbidden)
    bridge, _ = nika_windows.build_windows_bridge(AppConfig(database_path=tmp_path / "source.db"))
    assert bridge.get_state()["state"]["autostart"]["state"] == "unavailable"
    result = bridge.dispatch(
        {
            "request_id": "source",
            "action_id": "settings.autostart.configure",
            "payload": {"enabled": True},
        }
    )
    assert result["status"] == "rejected"


def test_packaged_bridge_wires_exact_host_executable_without_js_path_authority(
    tmp_path: Path, monkeypatch
) -> None:
    registration = Registration()
    exe = r"C:\Олексій\Nika Core\Nika.exe"
    monkeypatch.setattr(
        nika_windows, "sys", SimpleNamespace(platform="win32", frozen=True, executable=exe)
    )
    observed = []

    def service(path):
        observed.append(str(path))
        return WindowsAutostartService(path, registration)

    monkeypatch.setattr(nika_windows, "WindowsAutostartService", service)
    config = AppConfig(database_path=tmp_path / "packaged.db")
    bridge, _ = nika_windows.build_windows_bridge(config)
    actions = {item["action_id"]: item for item in bridge.list_actions()}
    assert actions["settings.autostart.configure"]["binding"] is None
    assert bridge.set_binding("settings.autostart.configure", "Ctrl+Alt+S")["ok"] is True
    result = bridge.dispatch(
        {
            "request_id": "packaged",
            "action_id": "settings.autostart.configure",
            "payload": {"enabled": True},
        }
    )
    assert result["request_id"] == "packaged" and result["status"] == "completed"
    assert observed == [exe]
    restarted, _ = nika_windows.build_windows_bridge(config)
    assert restarted.get_state()["state"]["autostart"]["state"] == "enabled"
    assert (
        restarted.dispatch(
            {"request_id": "refresh", "action_id": "settings.autostart.refresh", "payload": {}}
        )["status"]
        == "completed"
    )
    assert (
        restarted.dispatch(
            {
                "request_id": "bad",
                "action_id": "settings.autostart.refresh",
                "payload": {"enabled": False},
            }
        )["status"]
        == "rejected"
    )
    assert TaskQueue(SQLiteStore(config.database_path)).list_recent() == ()
