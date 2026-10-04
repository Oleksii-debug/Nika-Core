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


def _exact_registered_command(value: object) -> str:
    if type(value) is not str or not value:
        raise RuntimeError("Nika autostart registration is malformed")
    return value


class AutostartState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    STALE = "stale"


@dataclass(frozen=True)
class AutostartStatus:
    state: AutostartState
    registered_command: str | None
    # An equally named stale value of another registry kind is a new target.
    registration_type: str | None = None


class AutostartBackend(Protocol):
    def read(self) -> str | None: ...

    def write(self, command: str) -> None: ...

    def delete(self) -> None: ...


class _StaleAutostartRegistration(RuntimeError):
    def __init__(self, registered_command: str) -> None:
        super().__init__("Nika autostart registration uses a noncanonical string type")
        self.registered_command = registered_command


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
        if type(value_type) is not int:
            raise RuntimeError("Nika autostart registration has an unsupported value type")
        registered = _exact_registered_command(value)
        if value_type == winreg.REG_EXPAND_SZ:
            raise _StaleAutostartRegistration(registered)
        if value_type != winreg.REG_SZ:
            raise RuntimeError("Nika autostart registration has an unsupported value type")
        return registered

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
        self._backend = backend if backend is not None else WindowsRunKeyBackend()

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
        try:
            registered = self._backend.read()
        except _StaleAutostartRegistration as exc:
            return AutostartStatus(
                AutostartState.STALE,
                _exact_registered_command(exc.registered_command),
                "REG_EXPAND_SZ",
            )
        if registered is None:
            return AutostartStatus(AutostartState.DISABLED, None)
        registered = _exact_registered_command(registered)
        try:
            registered_units = _windows_utf16_code_units(registered)
        except ValueError:
            return AutostartStatus(AutostartState.STALE, registered, "REG_SZ")
        if registered_units > _MAX_RUN_COMMAND_LENGTH:
            return AutostartStatus(AutostartState.STALE, registered, "REG_SZ")
        expected = self.expected_command
        try:
            expected_units = _windows_utf16_code_units(expected)
        except ValueError:
            return AutostartStatus(AutostartState.STALE, registered, "REG_SZ")
        if expected_units > _MAX_RUN_COMMAND_LENGTH:
            return AutostartStatus(AutostartState.STALE, registered, "REG_SZ")
        if registered == expected:
            return AutostartStatus(AutostartState.ENABLED, registered, "REG_SZ")
        return AutostartStatus(AutostartState.STALE, registered, "REG_SZ")

    def _current_for_change(
        self, observed: AutostartStatus | None
    ) -> AutostartStatus:
        current = self.status()
        if observed is not None and current != observed:
            raise RuntimeError("Nika autostart registration changed before update")
        return current

    def enable(
        self, *, observed: AutostartStatus | None = None
    ) -> AutostartStatus:
        command = self._validated_command()
        current = self._current_for_change(observed)
        if current.state is AutostartState.ENABLED:
            return current
        self._backend.write(command)
        verified = self.status()
        if verified.state is not AutostartState.ENABLED:
            raise RuntimeError("Nika autostart registration did not verify after write")
        return verified

    def disable(
        self, *, observed: AutostartStatus | None = None
    ) -> AutostartStatus:
        # Recheck the state observed before the audit: another Nika instance
        # or process must not silently replace the target during the audit gap.
        # Windows Run-key APIs offer no atomic compare-and-delete operation.
        self._current_for_change(observed)
        self._backend.delete()
        verified = self.status()
        if verified.state is not AutostartState.DISABLED:
            raise RuntimeError("Nika autostart registration did not clear")
        return verified
