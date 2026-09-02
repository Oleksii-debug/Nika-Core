from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.windows_autostart import AutostartState, WindowsAutostartService


class FakeBackend:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.writes: list[str] = []
        self.deletes = 0

    def read(self) -> str | None:
        return self.value

    def write(self, command: str) -> None:
        self.writes.append(command)
        self.value = command

    def delete(self) -> None:
        self.deletes += 1
        self.value = None


def test_enable_disable_and_readback_are_idempotent() -> None:
    backend = FakeBackend()
    service = WindowsAutostartService(Path(r"C:\Program Files\Nika\Nika Core.exe"), backend)

    assert service.status().state is AutostartState.DISABLED
    assert service.enable().state is AutostartState.ENABLED
    assert service.enable().state is AutostartState.ENABLED
    assert backend.writes == ['"C:\\Program Files\\Nika\\Nika Core.exe"']

    assert service.disable().state is AutostartState.DISABLED
    assert service.disable().state is AutostartState.DISABLED
    assert backend.deletes == 2


def test_unicode_and_space_path_is_quoted_as_one_executable() -> None:
    backend = FakeBackend()
    service = WindowsAutostartService(
        Path(r"C:\Users\Олексій\Nika Core\Nika.exe"),
        backend,
    )

    service.enable()

    assert backend.value == '"C:\\Users\\Олексій\\Nika Core\\Nika.exe"'


def test_changed_install_path_is_stale_until_explicit_reenable() -> None:
    backend = FakeBackend('"C:\\Old Nika\\Nika.exe"')
    service = WindowsAutostartService(Path(r"C:\New Nika\Nika.exe"), backend)

    status = service.status()
    assert status.state is AutostartState.STALE
    assert status.registered_command == '"C:\\Old Nika\\Nika.exe"'

    assert service.enable().state is AutostartState.ENABLED
    assert backend.value == '"C:\\New Nika\\Nika.exe"'


def test_malformed_or_foreign_command_is_never_reported_enabled() -> None:
    backend = FakeBackend('cmd.exe /c "C:\\Program Files\\Nika\\Nika.exe"')
    service = WindowsAutostartService(Path(r"C:\Program Files\Nika\Nika.exe"), backend)

    assert service.status().state is AutostartState.STALE


def test_relative_executable_path_fails_closed() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        WindowsAutostartService(Path("Nika.exe"), FakeBackend())


def _service_with_command_length(length: int, backend: FakeBackend) -> WindowsAutostartService:
    prefix = "C:\\"
    suffix = ".exe"
    path = prefix + ("a" * (length - len(prefix) - len(suffix))) + suffix
    service = WindowsAutostartService(Path(path), backend)
    assert len(service.expected_command) == length
    return service


def test_run_command_exactly_260_characters_is_allowed() -> None:
    backend = FakeBackend()
    service = _service_with_command_length(260, backend)

    assert service.enable().state is AutostartState.ENABLED
    assert backend.writes == [service.expected_command]


def test_run_command_over_260_characters_fails_before_write() -> None:
    backend = FakeBackend()
    service = _service_with_command_length(261, backend)

    with pytest.raises(ValueError, match="260-character limit"):
        service.enable()

    assert backend.writes == []
    assert backend.value is None


def test_legacy_oversized_registration_is_never_reported_enabled() -> None:
    backend = FakeBackend()
    service = _service_with_command_length(261, backend)
    backend.value = service.expected_command

    status = service.status()

    assert status.state is AutostartState.STALE
    assert status.registered_command == service.expected_command
    with pytest.raises(ValueError, match="260-character limit"):
        service.enable()
    assert backend.writes == []
    assert backend.value == service.expected_command
