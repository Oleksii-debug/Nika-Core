from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic_settings import SettingsError

from nika_core.config import AppConfig
from nika_core.ui import shell as ui_shell
from scripts import nika_windows


@pytest.fixture(autouse=True)
def _shell_preflight_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nika_windows, "preflight_windows_shell", lambda: None)


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
        "build_windows_bridge",
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
        "build_windows_bridge",
        lambda _config: pytest.fail("runtime must not start after source failure"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "PRIVATE_SETTINGS_SOURCE_CANARY" not in messages[0]
    assert "Некоректні налаштування" in messages[0]


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.DatabaseError("PRIVATE_DATABASE_CANARY"),
        OSError("PRIVATE_STORAGE_PATH_CANARY"),
        RuntimeError("PRIVATE_NEWER_SCHEMA_CANARY"),
    ],
)
def test_storage_startup_failure_is_accessible_private_and_does_not_launch_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    messages: list[str] = []

    def fail_open(_config: AppConfig) -> None:
        raise failure

    monkeypatch.setattr(nika_windows, "build_windows_bridge", fail_open)
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)
    monkeypatch.setattr(
        nika_windows,
        "launch_windows_shell",
        lambda *_args, **_kwargs: pytest.fail("shell must not open on storage error"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "Не вдалося відкрити дані" in messages[0]
    assert "PRIVATE_" not in messages[0]
    assert not config.database_path.exists()


def test_actual_corrupt_database_is_not_overwritten_during_failed_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "База даних" / "ніка.db"
    database.parent.mkdir()
    original_bytes = b"PRIVATE_CORRUPT_SQLITE_CANARY"
    database.write_bytes(original_bytes)
    config = AppConfig(database_path=database)
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)
    monkeypatch.setattr(
        nika_windows,
        "launch_windows_shell",
        lambda *_args, **_kwargs: pytest.fail("corrupt database must not open shell"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "PRIVATE_" not in messages[0]
    assert database.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("PRIVATE_UI_RESOURCE_PATH_CANARY"),
        ImportError("PRIVATE_PYWEBVIEW_IMPORT_CANARY"),
    ],
)
def test_shell_preflight_failure_happens_before_runtime_recovery_and_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def fail_preflight() -> None:
        raise failure

    monkeypatch.setattr(nika_windows, "preflight_windows_shell", fail_preflight)
    monkeypatch.setattr(
        nika_windows,
        "build_windows_bridge",
        lambda _config: pytest.fail("runtime recovery must not start before shell preflight"),
    )
    monkeypatch.setattr(
        nika_windows,
        "launch_windows_shell",
        lambda *_args, **_kwargs: pytest.fail("shell must not launch after failed preflight"),
    )

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "Не вдалося підготувати інтерфейс Nika" in messages[0]
    assert "не відновлювалися" in messages[0]
    assert "PRIVATE_" not in messages[0]
    assert not config.database_path.exists()


def test_shell_preflight_checks_assets_before_importing_pywebview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("window.NIKA = true;", encoding="utf-8")
    imported: list[str] = []
    monkeypatch.setattr(ui_shell, "web_asset_root", lambda: tmp_path)
    monkeypatch.setattr(ui_shell, "import_module", lambda name: imported.append(name))

    with pytest.raises(FileNotFoundError):
        ui_shell.preflight_windows_shell()

    assert imported == []


def test_shell_preflight_imports_pywebview_after_complete_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("index.html", "app.js", "styles.css"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    imported: list[str] = []
    monkeypatch.setattr(ui_shell, "web_asset_root", lambda: tmp_path)
    monkeypatch.setattr(ui_shell, "import_module", lambda name: imported.append(name))

    ui_shell.preflight_windows_shell()

    assert imported == ["webview"]


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("PRIVATE_UI_RESOURCE_PATH_CANARY"),
        RuntimeError("PRIVATE_WEBVIEW2_STARTUP_CANARY"),
    ],
)
def test_shell_launch_failure_is_accessible_private_and_returns_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    bridge = object()
    products = object()
    monkeypatch.setattr(
        nika_windows,
        "build_windows_bridge",
        lambda _config: (bridge, products),
    )
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def fail_shell(actual_bridge: object, *, title: str) -> None:
        assert actual_bridge is bridge
        assert title == f"Nika Core {config.app_version}"
        raise failure

    monkeypatch.setattr(nika_windows, "launch_windows_shell", fail_shell)

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "Не вдалося відкрити інтерфейс Nika" in messages[0]
    assert "PRIVATE_" not in messages[0]


def test_shell_launch_boundary_does_not_swallow_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    monkeypatch.setattr(
        nika_windows,
        "build_windows_bridge",
        lambda _config: (object(), object()),
    )

    def stop_process(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(73)

    monkeypatch.setattr(nika_windows, "launch_windows_shell", stop_process)

    with pytest.raises(SystemExit) as caught:
        nika_windows.main([])
    assert caught.value.code == 73

