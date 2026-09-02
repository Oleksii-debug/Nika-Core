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


def test_run_key_command_over_260_characters_fails_before_write() -> None:
    """Registry readback is not proof Windows accepts an oversized Run command."""
    backend = RecordingBackend()
    executable = Path("C:\\" + ("a" * 255) + ".exe")
    service = WindowsAutostartService(executable, backend)
    assert len(service.expected_command) > 260

    with pytest.raises(ValueError, match="260"):
        service.enable()

    assert backend.writes == []
