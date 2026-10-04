from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.ui.autostart_settings import AutostartSettings
from nika_core.windows_autostart import WindowsAutostartService, WindowsRunKeyBackend


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None


class _Registry:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, value: str, value_type: int) -> None:
        self.value: str | None = value
        self.value_type = value_type
        self.writes = 0
        self.deletes = 0

    def OpenKey(self, _root, _path, *_args):
        return _Key()

    def CreateKeyEx(self, _root, _path, _reserved, _access):
        return _Key()

    def QueryValueEx(self, _key, _name):
        if self.value is None:
            raise FileNotFoundError("value not present")
        return self.value, self.value_type

    def SetValueEx(self, _key, _name, _reserved, value_type, value) -> None:
        self.writes += 1
        self.value_type = value_type
        self.value = value

    def DeleteValue(self, _key, _name) -> None:
        self.deletes += 1
        if self.value is None:
            raise FileNotFoundError("value not present")
        self.value = None


class _Backend(WindowsRunKeyBackend):
    def __init__(self, registry: _Registry) -> None:
        self.registry = registry

    def _winreg(self):
        return self.registry


def test_expandable_registry_value_requires_explicit_ui_repair_and_restart(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    registry = _Registry("", _Registry.REG_EXPAND_SZ)
    path = Path(r"C:\Users\Олексій\Nika Core\Nika.exe")
    service = WindowsAutostartService(path, _Backend(registry))
    registry.value = service.expected_command
    settings = AutostartSettings(service, audit)

    # The expanded registry type must not be trusted, even with identical text.
    initial = settings.snapshot()
    assert initial["state"] == "stale"
    assert initial["can_change"] is True
    assert registry.writes == 0 and registry.deletes == 0
    assert "Олексій" not in json.dumps(initial, ensure_ascii=False)

    # Re-reading and reconstructing the adapter must never silently repair it.
    restarted = AutostartSettings(WindowsAutostartService(path, _Backend(registry)), audit)
    assert restarted.refresh({}).status == "completed"
    assert restarted.snapshot()["state"] == "stale"
    assert registry.value_type == _Registry.REG_EXPAND_SZ
    assert registry.writes == 0

    # An explicit Boolean setting is the only path that repairs the registration.
    result = restarted.configure({"enabled": True})
    assert result.status == "completed"
    assert result.focus_id == "autostart-enabled"
    assert registry.writes == 1
    assert registry.value_type == _Registry.REG_SZ
    assert registry.value == service.expected_command
    assert settings.snapshot()["state"] == "enabled"
    assert restarted.configure({"enabled": True}).status == "completed"
    assert registry.writes == 1

    assert settings.configure({"enabled": False}).status == "completed"
    assert restarted.snapshot()["state"] == "disabled"
    assert registry.deletes == 1
    assert TaskQueue(store).list_recent() == ()

    events = audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )
    assert [event.event_type for event in events] == [
        "settings.autostart.requested",
        "settings.autostart.confirmed",
        "settings.autostart.requested",
        "settings.autostart.confirmed",
        "settings.autostart.requested",
        "settings.autostart.confirmed",
    ]
    assert [event.payload for event in events] == [
        {"enabled": True},
        {"enabled": True},
        {"enabled": True},
        {"enabled": True},
        {"enabled": False},
        {"enabled": False},
    ]
    assert "Олексій" not in json.dumps(
        [event.payload for event in events], ensure_ascii=False
    )


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        ("PRIVATE_REGISTRY_CANARY", 255),
        ("", _Registry.REG_SZ),
        ("", _Registry.REG_EXPAND_SZ),
    ],
)
def test_malformed_registry_cannot_be_mutated_through_ui_or_service(
    tmp_path: Path, value: str, value_type: int
) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    registry = _Registry(value, value_type)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    settings = AutostartSettings(service, audit)

    snapshot = settings.snapshot()
    assert snapshot["state"] == "error"
    assert snapshot["can_change"] is False
    assert "PRIVATE_REGISTRY_CANARY" not in json.dumps(snapshot)
    assert settings.refresh({}).status == "failed"

    # A forged UI command must not bypass the disabled control.
    for enabled in (False, True):
        result = settings.configure({"enabled": enabled})
        assert result.status == "failed"
        assert result.focus_id == "autostart-enabled"
        assert "PRIVATE_REGISTRY_CANARY" not in result.model_dump_json()

    # The service itself must also reject direct deletes of malformed values.
    with pytest.raises(RuntimeError):
        service.disable()
    with pytest.raises(RuntimeError):
        service.enable()

    assert registry.value == value and registry.value_type == value_type
    assert registry.writes == 0 and registry.deletes == 0
    assert audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    ) == ()


def test_expandable_registration_remains_explicitly_removable(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    registry = _Registry("", _Registry.REG_EXPAND_SZ)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    registry.value = service.expected_command
    settings = AutostartSettings(service, audit)

    assert settings.snapshot()["state"] == "stale"
    assert settings.configure({"enabled": False}).status == "completed"
    assert settings.snapshot()["state"] == "disabled"
    assert registry.deletes == 1 and registry.writes == 0
    events = audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )
    assert [event.event_type for event in events] == [
        "settings.autostart.requested",
        "settings.autostart.confirmed",
    ]
