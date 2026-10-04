from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_settings import SettingsError

from nika_core.config import AppConfig
from scripts import nika_windows


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NIKA_SCHEMA_VERSION", "99"),
        ("NIKA_DB_PATH", "relative-private-path.db"),
        ("NIKA_LOG_LEVEL", "PRIVATE_LOG_LEVEL_CANARY"),
    ],
)
def test_invalid_packaged_environment_fails_before_runtime_and_preserves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    database = tmp_path / "Приватні дані" / "ніка.db"
    monkeypatch.setenv("NIKA_DB_PATH", str(database))
    monkeypatch.setenv("NIKA_SCHEMA_VERSION", "1")
    monkeypatch.setenv("NIKA_LOG_LEVEL", "INFO")
    monkeypatch.setenv(name, value)
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)
    monkeypatch.setattr(
        nika_windows,
        "build_windows_session",
        lambda _config: pytest.fail("runtime must not start with invalid configuration"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "Некоректні налаштування" in messages[0]
    assert value not in messages[0]
    assert not database.exists()


def test_settings_source_failure_does_not_expose_private_values_or_start_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_source(_cls: type[AppConfig]) -> AppConfig:
        raise SettingsError("PRIVATE_SETTINGS_SOURCE_CANARY")

    messages: list[str] = []
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(invalid_source))
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)
    monkeypatch.setattr(
        nika_windows,
        "build_windows_session",
        lambda _config: pytest.fail("runtime must not start after source failure"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "PRIVATE_SETTINGS_SOURCE_CANARY" not in messages[0]
    assert "Некоректні налаштування" in messages[0]
