from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika_core.config import AppConfig
from nika_core.ui import startup_error
from scripts import nika_windows


def _capture_messages(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(startup_error, "show_recovery_error", messages.append)
    return messages


@pytest.mark.parametrize("error", [ValueError("private-token"), OSError("private-path")])
def test_bad_configuration_displays_safe_error_before_session_creation(
    monkeypatch, error
) -> None:
    messages = _capture_messages(monkeypatch)

    def bad_config():
        raise error

    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(bad_config)
    )
    session_builder = Mock(side_effect=AssertionError("session must not start"))
    monkeypatch.setattr(nika_windows, "build_windows_session", session_builder)
    shell = Mock(side_effect=AssertionError("shell must not start"))
    monkeypatch.setattr(nika_windows, "launch_windows_shell", shell)

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "налаштування" in messages[0]
    assert "private-" not in messages[0]
    session_builder.assert_not_called()
    shell.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.DatabaseError("private-database"),
        OSError("private-path"),
        ValueError("private-metadata"),
    ],
)
def test_failed_session_initialization_reports_error_without_launching_shell(
    monkeypatch, tmp_path, error
) -> None:
    messages = _capture_messages(monkeypatch)
    config = AppConfig(database_path=tmp_path / "Ніка" / "nika.db")
    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(lambda: config)
    )
    builder = Mock(side_effect=error)
    shell = Mock(side_effect=AssertionError("shell must not start"))
    monkeypatch.setattr(nika_windows, "build_windows_session", builder)
    monkeypatch.setattr(nika_windows, "launch_windows_shell", shell)

    assert nika_windows.main([]) == 1
    builder.assert_called_once_with(config)
    shell.assert_not_called()
    assert len(messages) == 1
    assert "локальні дані" in messages[0]
    assert "private-" not in messages[0]


def test_uncertain_recovery_still_uses_specialized_fail_closed_message(
    monkeypatch, tmp_path
) -> None:
    messages = _capture_messages(monkeypatch)
    config = AppConfig(database_path=tmp_path / "nika.db")
    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(lambda: config)
    )
    monkeypatch.setattr(
        nika_windows,
        "build_windows_session",
        Mock(side_effect=nika_windows._StartupRecoveryInventoryError("private-marker")),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "незавершену роботу" in messages[0]
    assert "private-" not in messages[0]


@pytest.mark.parametrize("error", [OSError("private-dll"), RuntimeError("private-ui")])
def test_shell_failure_reports_safe_error_and_closes_session_once(
    monkeypatch, tmp_path, error
) -> None:
    messages = _capture_messages(monkeypatch)
    config = AppConfig(database_path=tmp_path / "nika.db")
    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(lambda: config)
    )
    session = SimpleNamespace(bridge=object(), close=Mock())
    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _: session)
    shell = Mock(side_effect=error)
    monkeypatch.setattr(nika_windows, "launch_windows_shell", shell)

    assert nika_windows.main([]) == 1
    shell.assert_called_once_with(session.bridge, title=f"Nika Core {config.app_version}")
    session.close.assert_called_once_with()
    assert len(messages) == 1
    assert "вікно" in messages[0]
    assert "private-" not in messages[0]


def test_proof_mode_keeps_failures_visible_to_ci(monkeypatch, tmp_path) -> None:
    messages = _capture_messages(monkeypatch)
    config = AppConfig(database_path=tmp_path / "nika.db")
    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(lambda: config)
    )
    proof = Mock(side_effect=sqlite3.DatabaseError("proof failure"))
    monkeypatch.setattr(nika_windows, "_run_pf11_proof", proof)

    with pytest.raises(sqlite3.DatabaseError, match="proof failure"):
        nika_windows.main(["--pf11-proof"])
    proof.assert_called_once()
    assert messages == []
