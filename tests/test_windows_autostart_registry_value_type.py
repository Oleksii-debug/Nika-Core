from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.windows_autostart import (
    AutostartState,
    WindowsAutostartService,
    WindowsRunKeyBackend,
)


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, value: str, value_type: int) -> None:
        self.value = value
        self.value_type = value_type

    def OpenKey(self, *_args):
        return _Key()

    def QueryValueEx(self, _key, _name):
        return self.value, self.value_type


class _Backend(WindowsRunKeyBackend):
    def __init__(self, winreg) -> None:
        self._fake_winreg = winreg

    def _winreg(self):
        return self._fake_winreg


def test_exact_reg_sz_value_can_match_percent_containing_executable() -> None:
    fake = _FakeWinreg("", _FakeWinreg.REG_SZ)
    service = WindowsAutostartService(Path(r"C:\%NIKA_HOME%\Nika.exe"), _Backend(fake))
    fake.value = service.expected_command

    status = service.status()

    assert status.state is AutostartState.ENABLED
    assert status.registered_command == service.expected_command


def test_expandable_value_never_authorizes_same_visible_command() -> None:
    fake = _FakeWinreg("", _FakeWinreg.REG_EXPAND_SZ)
    service = WindowsAutostartService(Path(r"C:\%NIKA_HOME%\Nika.exe"), _Backend(fake))
    fake.value = service.expected_command

    with pytest.raises(RuntimeError, match="unsupported value type"):
        service.status()


def test_noncanonical_registry_type_carrier_is_rejected() -> None:
    class IntSubclass(int):
        pass

    fake = _FakeWinreg(r"C:\Nika\Nika.exe", IntSubclass(_FakeWinreg.REG_SZ))
    backend = _Backend(fake)

    with pytest.raises(RuntimeError, match="unsupported value type"):
        backend.read()
