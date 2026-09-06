from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "NikaCore"
_MAX_RUN_COMMAND_LENGTH = 260


def _windows_utf16_code_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ValueError("Autostart command is not valid Windows UTF-16 text") from None


class AutostartState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    STALE = "stale"


@dataclass(frozen=True)
class AutostartStatus:
    state: AutostartState
    registered_command: str | None


class AutostartBackend(Protocol):
    def read(self) -> str | None: ...

    def write(self, command: str) -> None: ...

    def delete(self) -> None: ...


class WindowsRunKeyBackend:
    """Per-user Windows Run-key storage. Never requests elevation."""

    def _winreg(self):  # type: ignore[no-untyped-def]
        if os.name != "nt":
            raise OSError("Windows autostart is available only on Windows")
        import winreg

        return winreg

    def read(self) -> str | None:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, value_type = winreg.QueryValueEx(key, _VALUE_NAME)
        except FileNotFoundError:
            return None
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
            raise RuntimeError("Nika autostart registration has an unsupported value type")
        if not isinstance(value, str) or not value:
            raise RuntimeError("Nika autostart registration is malformed")
        return value

    def write(self, command: str) -> None:
        winreg = self._winreg()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, _VALUE_NAME)
        except FileNotFoundError:
            return


class WindowsAutostartService:
    """Backend authority for the user-controlled Windows login-start setting.

    This service only owns registration. It grants no task/tool permission and
    intentionally does not perform runtime recovery or task execution.
    """

    def __init__(self, executable: Path, backend: AutostartBackend | None = None) -> None:
        executable_text = str(executable.expanduser())
        if "\x00" in executable_text:
            raise ValueError("Autostart executable path must not contain NUL")
        if not PureWindowsPath(executable_text).is_absolute():
            raise ValueError("Autostart executable path must be absolute")
        self._executable = executable_text
        self._backend = backend or WindowsRunKeyBackend()

    @property
    def expected_command(self) -> str:
        return subprocess.list2cmdline([self._executable])

    def _validated_command(self) -> str:
        command = self.expected_command
        if _windows_utf16_code_units(command) > _MAX_RUN_COMMAND_LENGTH:
            raise ValueError(
                "Autostart command exceeds the Windows Run-key 260-character limit"
            )
        return command

    def status(self) -> AutostartStatus:
        registered = self._backend.read()
        if registered is None:
            return AutostartStatus(AutostartState.DISABLED, None)
        try:
            registered_units = _windows_utf16_code_units(registered)
        except ValueError:
            return AutostartStatus(AutostartState.STALE, registered)
        if registered_units > _MAX_RUN_COMMAND_LENGTH:
            return AutostartStatus(AutostartState.STALE, registered)
        expected = self.expected_command
        try:
            expected_units = _windows_utf16_code_units(expected)
        except ValueError:
            return AutostartStatus(AutostartState.STALE, registered)
        if expected_units > _MAX_RUN_COMMAND_LENGTH:
            return AutostartStatus(AutostartState.STALE, registered)
        if registered == expected:
            return AutostartStatus(AutostartState.ENABLED, registered)
        return AutostartStatus(AutostartState.STALE, registered)

    def enable(self) -> AutostartStatus:
        command = self._validated_command()
        current = self.status()
        if current.state is AutostartState.ENABLED:
            return current
        self._backend.write(command)
        verified = self.status()
        if verified.state is not AutostartState.ENABLED:
            raise RuntimeError("Nika autostart registration did not verify after write")
        return verified

    def disable(self) -> AutostartStatus:
        self._backend.delete()
        verified = self.status()
        if verified.state is not AutostartState.DISABLED:
            raise RuntimeError("Nika autostart registration did not clear")
        return verified
