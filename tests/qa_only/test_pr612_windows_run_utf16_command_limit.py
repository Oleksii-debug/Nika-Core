"""QA_ONLY regression for the Windows Run-key UTF-16 command-length boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.windows_autostart import WindowsAutostartService


class RecordingBackend:
    def __init__(self) -> None:
        self.value: str | None = None
        self.writes: list[str] = []

    def read(self) -> str | None:
        return self.value

    def write(self, command: str) -> None:
        self.writes.append(command)
        self.value = command

    def delete(self) -> None:
        self.value = None


def test_astral_unicode_run_command_over_utf16_limit_fails_before_write() -> None:
    """Windows command storage is UTF-16 even when Python counts one code point."""
    backend = RecordingBackend()
    executable = Path("C:\\" + ("\U0001f600" * 253) + ".exe")
    service = WindowsAutostartService(executable, backend)
    command = service.expected_command

    assert len(command) == 260
    assert len(command.encode("utf-16-le")) // 2 > 260

    with pytest.raises(ValueError, match="260"):
        service.enable()

    assert backend.writes == []
    assert backend.value is None
