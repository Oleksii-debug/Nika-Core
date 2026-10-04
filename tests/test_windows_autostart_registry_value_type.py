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

    def write(self, command: str) -> None:
        self._fake_winreg.value = command
        self._fake_winreg.value_type = self._fake_winreg.REG_SZ


class _BehavioralStr(str):
    encode_calls = 0
    equality_calls = 0

    def encode(self, *args, **kwargs):
        type(self).encode_calls += 1
        raise AssertionError("behavioral encode must not execute")

    def __eq__(self, other):
        type(self).equality_calls += 1
        raise AssertionError("behavioral equality must not execute")


class _ProtocolBackend:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def __bool__(self) -> bool:
        raise AssertionError("backend truthiness must not be evaluated")

    def read(self) -> str | None:
        return self.value

    def write(self, command: str) -> None:
        self.value = command

    def delete(self) -> None:
        self.value = None


def test_exact_reg_sz_value_can_match_percent_containing_executable() -> None:
    fake = _FakeWinreg("", _FakeWinreg.REG_SZ)
    service = WindowsAutostartService(Path(r"C:\%NIKA_HOME%\Nika.exe"), _Backend(fake))
    fake.value = service.expected_command

    status = service.status()

    assert status.state is AutostartState.ENABLED
    assert status.registered_command == service.expected_command


def test_expandable_value_is_stale_and_explicit_enable_repairs_it() -> None:
    fake = _FakeWinreg("", _FakeWinreg.REG_EXPAND_SZ)
    service = WindowsAutostartService(Path(r"C:\%NIKA_HOME%\Nika.exe"), _Backend(fake))
    fake.value = service.expected_command

    status = service.status()

    assert status.state is AutostartState.STALE
    assert status.registered_command == service.expected_command
    assert service.enable().state is AutostartState.ENABLED
    assert fake.value_type == _FakeWinreg.REG_SZ


def test_noncanonical_registry_type_carrier_is_rejected() -> None:
    class IntSubclass(int):
        pass

    fake = _FakeWinreg(r"C:\Nika\Nika.exe", IntSubclass(_FakeWinreg.REG_SZ))
    backend = _Backend(fake)

    with pytest.raises(RuntimeError, match="unsupported value type"):
        backend.read()


@pytest.mark.parametrize("value_type", [_FakeWinreg.REG_SZ, _FakeWinreg.REG_EXPAND_SZ])
def test_behavioral_registry_text_is_rejected_before_text_behavior(value_type: int) -> None:
    _BehavioralStr.encode_calls = 0
    _BehavioralStr.equality_calls = 0
    fake = _FakeWinreg(_BehavioralStr(r"C:\Nika\Nika.exe"), value_type)
    backend = _Backend(fake)

    with pytest.raises(RuntimeError, match="registration is malformed"):
        backend.read()

    assert _BehavioralStr.encode_calls == 0
    assert _BehavioralStr.equality_calls == 0


def test_protocol_backend_text_is_exact_fenced_before_status_text_behavior() -> None:
    _BehavioralStr.encode_calls = 0
    _BehavioralStr.equality_calls = 0
    backend = _ProtocolBackend(_BehavioralStr(r"C:\Nika\Nika.exe"))
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), backend)

    with pytest.raises(RuntimeError, match="registration is malformed"):
        service.status()

    assert _BehavioralStr.encode_calls == 0
    assert _BehavioralStr.equality_calls == 0


def test_explicit_backend_is_selected_without_truthiness_dispatch() -> None:
    backend = _ProtocolBackend()
    service = WindowsAutostartService(Path(r"C:\Nika\Nika.exe"), backend)

    assert service.status().state is AutostartState.DISABLED
