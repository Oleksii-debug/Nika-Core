from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.ui.autostart_settings import AutostartSettings
from nika_core.windows_autostart import WindowsAutostartService, WindowsRunKeyBackend
from scripts import nika_windows


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


def test_packaged_bridge_rejects_forged_disable_on_unreadable_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _Registry("PRIVATE_REGISTRY_CANARY", 255)
    executable = r"C:\Users\Олексій\Nika Core\Nika.exe"
    monkeypatch.setattr(
        nika_windows,
        "sys",
        SimpleNamespace(platform="win32", frozen=True, executable=executable),
    )

    def registered_service(path: Path) -> WindowsAutostartService:
        assert str(path) == executable
        return WindowsAutostartService(path, _Backend(registry))

    monkeypatch.setattr(nika_windows, "WindowsAutostartService", registered_service)
    config = AppConfig(database_path=tmp_path / "bridge.db")
    bridge, _ = nika_windows.build_windows_bridge(config)

    snapshot = bridge.get_state()["state"]["autostart"]
    assert snapshot["state"] == "error"
    assert snapshot["can_change"] is False

    for enabled in (False, True):
        result = bridge.dispatch(
            {
                "request_id": "forged-autostart",
                "action_id": "settings.autostart.configure",
                "payload": {"enabled": enabled},
            }
        )
        assert result["status"] == "failed"
        assert result["request_id"] == "forged-autostart"
        assert "PRIVATE_REGISTRY_CANARY" not in json.dumps(result)

    assert registry.value == "PRIVATE_REGISTRY_CANARY"
    assert registry.value_type == 255
    assert registry.deletes == 0 and registry.writes == 0
    assert AuditLog(SQLiteStore(config.database_path)).list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    ) == ()


def test_packaged_uia_proof_checks_exact_run_key_type_and_presence() -> None:
    proof = (Path(__file__).resolve().parents[1] / "scripts/m5_uia_proof.ps1").read_text(
        encoding="utf-8"
    )
    preflight = proof.index("    if ($AutostartPhase -ne 'None') {")
    launch = proof.index("    $process = Start-Process", preflight)
    expected_kind = "[Microsoft.Win32.RegistryValueKind]::String"
    preflight_text = proof[preflight:launch]
    assert "$existingPresent = " in preflight_text
    assert "$runKey.GetValueNames() -ccontains 'NikaCore'" in preflight_text
    assert "$runKey.GetValueKind('NikaCore')" in preflight_text
    assert "$AutostartPhase -eq 'Enable' -and $existingPresent" in preflight_text
    assert "$existingKind -ne " + expected_kind in preflight_text

    readback = proof.index("        $actualPresent = ", launch)
    receipt = proof.index("        Write-Host \"Packaged autostart phase ", readback)
    receipt_text = proof[readback:receipt]
    assert "$runKey.GetValueNames() -ccontains 'NikaCore'" in receipt_text
    assert "$runKey.GetValueKind('NikaCore')" in receipt_text
    assert "$AutostartPhase -eq 'Disable' -and $actualPresent" in receipt_text
    assert "$actualKind -ne " + expected_kind in receipt_text


def test_hosted_proof_retry_and_cleanup_never_take_foreign_registry_type() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1] / "scripts/v01_autostart_uia_proof.ps1"
    ).read_text(encoding="utf-8")
    expected_kind = "[Microsoft.Win32.RegistryValueKind]::String"
    enable = wrapper.index("        $afterFailedEnablePresent = ")
    disable = wrapper.index("        $afterFailedDisablePresent = ", enable)
    cleanup = wrapper.index("    $currentPresent = ", disable)
    assert "$key.GetValueNames() -ccontains 'NikaCore'" in wrapper[enable:disable]
    assert (
        "$null -eq $afterFailedEnable -and -not $afterFailedEnablePresent"
        in wrapper[enable:disable]
    )
    disable_text = wrapper[disable:cleanup]
    assert "$key.GetValueKind('NikaCore')" in disable_text
    assert "$afterFailedDisableKind -eq " + expected_kind in disable_text
    assert "$key.GetValueNames() -ccontains 'NikaCore'" in wrapper[cleanup:]
    assert "$currentKind = if ($currentPresent)" in wrapper[cleanup:]
    assert "$currentKind -eq " + expected_kind in wrapper[cleanup:]
    assert "$key.DeleteValue('NikaCore', $false)" in wrapper[cleanup:]
    assert "elseif ($currentPresent)" in wrapper[cleanup:]


@pytest.mark.parametrize("enabled", [False, True])
def test_registration_changed_during_audit_cannot_be_overwritten_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    registry = _Registry("", _Registry.REG_SZ)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    registry.value = None if enabled else service.expected_command
    settings = AutostartSettings(service, audit)
    foreign_command = r"C:\Different\Foreign.exe"
    append = audit.append

    def change_after_requested_audit(**kwargs):
        result = append(**kwargs)
        if kwargs["event_type"] == "settings.autostart.requested":
            registry.value = foreign_command
        return result

    monkeypatch.setattr(audit, "append", change_after_requested_audit)
    assert settings.configure({"enabled": enabled}).status == "failed"
    assert registry.value == foreign_command
    assert registry.writes == 0 and registry.deletes == 0
    assert settings.snapshot()["state"] == "stale"
    events = audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )
    assert [event.event_type for event in events] == ["settings.autostart.requested"]
    assert events[0].payload == {"enabled": enabled}
    assert foreign_command not in json.dumps(events[0].payload)


@pytest.mark.parametrize("enabled", [False, True])
def test_direct_service_rejects_observed_registration_that_has_changed(
    enabled: bool,
) -> None:
    registry = _Registry("", _Registry.REG_SZ)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    registry.value = None if enabled else service.expected_command
    observed = service.status()
    registry.value = r"C:\Different\Foreign.exe"

    with pytest.raises(RuntimeError, match="changed before update"):
        if enabled:
            service.enable(observed=observed)
        else:
            service.disable(observed=observed)
    assert registry.writes == 0 and registry.deletes == 0
    assert registry.value == r"C:\Different\Foreign.exe"

@pytest.mark.parametrize("enabled", [False, True])
def test_registry_kind_change_with_identical_text_during_audit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    foreign_command = r"C:\Different\Foreign.exe"
    registry = _Registry(foreign_command, _Registry.REG_EXPAND_SZ)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    settings = AutostartSettings(service, audit)
    observed = service.status()
    assert observed.state.value == "stale"
    assert observed.registration_type == "REG_EXPAND_SZ"
    append = audit.append

    def change_kind_after_requested_audit(**kwargs):
        result = append(**kwargs)
        if kwargs["event_type"] == "settings.autostart.requested":
            registry.value_type = _Registry.REG_SZ
        return result

    monkeypatch.setattr(audit, "append", change_kind_after_requested_audit)
    assert settings.configure({"enabled": enabled}).status == "failed"
    assert registry.value == foreign_command
    assert registry.value_type == _Registry.REG_SZ
    assert registry.writes == 0 and registry.deletes == 0
    assert service.status().registration_type == "REG_SZ"
    events = audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )
    assert [event.event_type for event in events] == ["settings.autostart.requested"]


@pytest.mark.parametrize("enabled", [False, True])
def test_direct_service_rejects_same_text_different_registry_kind(enabled: bool) -> None:
    foreign_command = r"C:\Different\Foreign.exe"
    registry = _Registry(foreign_command, _Registry.REG_EXPAND_SZ)
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    observed = service.status()
    registry.value_type = _Registry.REG_SZ

    with pytest.raises(RuntimeError, match="changed before update"):
        if enabled:
            service.enable(observed=observed)
        else:
            service.disable(observed=observed)
    assert registry.value == foreign_command
    assert registry.writes == 0 and registry.deletes == 0


def test_repeated_ui_disable_of_absent_registration_never_opens_mutating_key(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "autostart.db")
    store.initialize()
    audit = AuditLog(store)
    registry = _Registry("", _Registry.REG_SZ)
    registry.value = None

    def forbidden_delete(*_args):
        pytest.fail("Absent Run value must not be deleted")

    registry.DeleteValue = forbidden_delete
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), _Backend(registry))
    settings = AutostartSettings(service, audit)
    for _ in range(2):
        assert settings.configure({"enabled": False}).status == "completed"
    assert settings.snapshot()["state"] == "disabled"
    assert registry.writes == 0 and registry.deletes == 0
    assert [e.event_type for e in audit.list_for(
        entity_type="application_setting", entity_id="windows.autostart"
    )] == [
        "settings.autostart.requested", "settings.autostart.confirmed",
    ] * 2
