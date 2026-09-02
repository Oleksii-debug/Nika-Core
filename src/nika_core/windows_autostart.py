from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "NikaCore"


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
        expanded = executable.expanduser()
        if not expanded.is_absolute():
            raise ValueError("Autostart executable path must be absolute")
        self._executable = expanded
        self._backend = backend or WindowsRunKeyBackend()

    @property
    def expected_command(self) -> str:
        return subprocess.list2cmdline([str(self._executable)])

    def status(self) -> AutostartStatus:
        registered = self._backend.read()
        if registered is None:
            return AutostartStatus(AutostartState.DISABLED, None)
        if registered == self.expected_command:
            return AutostartStatus(AutostartState.ENABLED, registered)
        return AutostartStatus(AutostartState.STALE, registered)

    def enable(self) -> AutostartStatus:
        current = self.status()
        if current.state is AutostartState.ENABLED:
            return current
        self._backend.write(self.expected_command)
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
