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

    def fail_open(_config: AppConfig, **_kwargs: object) -> None:
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


def test_build_windows_bridge_can_disable_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")

    def fail_if_started(_self: object, **_kwargs: object) -> dict[str, object]:
        pytest.fail("startup recovery must stay disabled for proof composition")

    monkeypatch.setattr(
        nika_windows.DesktopBackend,
        "start_startup_recovery",
        fail_if_started,
    )

    bridge, products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )

    assert bridge is not None
    assert products is not None


def test_pf11_proof_requests_bridge_without_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    observed: list[bool] = []

    def capture_bridge(
        _config: AppConfig,
        *,
        start_startup_recovery: bool = True,
        **_kwargs: object,
    ) -> tuple[object, object]:
        observed.append(start_startup_recovery)
        raise RuntimeError("PROOF_BRIDGE_CAPTURE")

    monkeypatch.setattr(nika_windows, "build_windows_bridge", capture_bridge)

    with pytest.raises(RuntimeError, match="PROOF_BRIDGE_CAPTURE"):
        nika_windows._run_pf11_proof(
            config,
            command="Створи тестовий ProductProject",
            output_path=None,
        )

    assert observed == [False]


def test_bridge_composition_failure_happens_before_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    recovery_started: list[bool] = []

    def record_recovery(_self: object, **_kwargs: object) -> dict[str, object]:
        recovery_started.append(True)
        return {}

    def fail_product_service(_repository: object) -> object:
        raise RuntimeError("PRIVATE_POST_BACKEND_COMPOSITION_CANARY")

    monkeypatch.setattr(
        nika_windows.DesktopBackend,
        "start_startup_recovery",
        record_recovery,
    )
    monkeypatch.setattr(
        nika_windows,
        "ProductProjectCommandService",
        fail_product_service,
    )

    with pytest.raises(RuntimeError, match="PRIVATE_POST_BACKEND_COMPOSITION_CANARY"):
        nika_windows.build_windows_bridge(config)

    assert recovery_started == []


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
    caplog: pytest.LogCaptureFixture,
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
    assert "PRIVATE_" not in caplog.text
    assert f"exception_type={type(failure).__name__}" in caplog.text
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


def test_shell_deferred_startup_waits_for_loaded_webview_before_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    create_kwargs: dict[str, object] = {}

    class LoadedEvent:
        @staticmethod
        def wait(timeout: int) -> bool:
            assert timeout == 20
            events.append("loaded")
            return True

    class ShownEvent:
        @staticmethod
        def is_set() -> bool:
            return True

    class Events:
        loaded = LoadedEvent()
        shown = ShownEvent()

    class Window:
        events = Events()

        def show(self) -> None:
            events.append("show")

        def destroy(self) -> None:
            events.append("destroy")

    window = Window()

    class WebView:
        @staticmethod
        def create_window(_title: str, _url: str, **kwargs: object) -> Window:
            create_kwargs.update(kwargs)
            return window

        @staticmethod
        def start(func=None, *, gui: str) -> None:
            assert gui == "edgechromium"
            assert func is not None
            func()

    monkeypatch.setattr(ui_shell, "preflight_windows_shell", lambda: None)
    monkeypatch.setattr(ui_shell, "import_module", lambda _name: WebView)

    result = ui_shell.launch_windows_shell(
        object(),
        on_gui_started=lambda: events.append("recovery"),
    )

    assert result is window
    assert create_kwargs["hidden"] is True
    assert events == ["loaded", "recovery", "show"]


def test_shell_loaded_timeout_never_runs_recovery_and_destroys_hidden_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class LoadedEvent:
        @staticmethod
        def wait(timeout: int) -> bool:
            assert timeout == 20
            events.append("loaded-timeout")
            return False

    class ShownEvent:
        @staticmethod
        def is_set() -> bool:
            return False

    class Events:
        loaded = LoadedEvent()
        shown = ShownEvent()

    class Window:
        events = Events()

        def show(self) -> None:
            events.append("show")

        def destroy(self) -> None:
            events.append("destroy")

    window = Window()

    class WebView:
        @staticmethod
        def create_window(_title: str, _url: str, **_kwargs: object) -> Window:
            return window

        @staticmethod
        def start(func=None, *, gui: str) -> None:
            assert gui == "edgechromium"
            assert func is not None
            func()

    monkeypatch.setattr(ui_shell, "preflight_windows_shell", lambda: None)
    monkeypatch.setattr(ui_shell, "import_module", lambda _name: WebView)

    with pytest.raises(RuntimeError, match="did not reach loaded state"):
        ui_shell.launch_windows_shell(
            object(),
            on_gui_started=lambda: events.append("recovery"),
        )

    assert events == ["loaded-timeout", "destroy"]


def test_shell_deferred_startup_failure_destroys_hidden_window_and_is_rethrown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class LoadedEvent:
        @staticmethod
        def wait(timeout: int) -> bool:
            assert timeout == 20
            events.append("loaded")
            return True

    class ShownEvent:
        @staticmethod
        def is_set() -> bool:
            return False

    class Events:
        loaded = LoadedEvent()
        shown = ShownEvent()

    class Window:
        events = Events()

        def show(self) -> None:
            events.append("show")

        def destroy(self) -> None:
            events.append("destroy")

    window = Window()

    class WebView:
        @staticmethod
        def create_window(_title: str, _url: str, **_kwargs: object) -> Window:
            return window

        @staticmethod
        def start(func=None, *, gui: str) -> None:
            assert gui == "edgechromium"
            assert func is not None
            func()

    def fail_recovery() -> None:
        events.append("recovery")
        raise RuntimeError("DEFERRED_RECOVERY_CANARY")

    monkeypatch.setattr(ui_shell, "preflight_windows_shell", lambda: None)
    monkeypatch.setattr(ui_shell, "import_module", lambda _name: WebView)

    with pytest.raises(RuntimeError, match="DEFERRED_RECOVERY_CANARY"):
        ui_shell.launch_windows_shell(
            object(),
            on_gui_started=fail_recovery,
        )

    assert events == ["loaded", "recovery", "destroy"]


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    bridge = object()
    products = object()

    def build(
        _config: AppConfig,
        *,
        defer_startup_recovery,
        **_kwargs: object,
    ) -> tuple[object, object]:
        defer_startup_recovery(lambda: None)
        return bridge, products

    monkeypatch.setattr(nika_windows, "build_windows_bridge", build)
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def fail_shell(
        actual_bridge: object,
        *,
        title: str,
        on_gui_started=None,
    ) -> None:
        assert actual_bridge is bridge
        assert title == f"Nika Core {config.app_version}"
        assert on_gui_started is not None
        raise failure

    monkeypatch.setattr(nika_windows, "launch_windows_shell", fail_shell)

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "Не вдалося відкрити інтерфейс Nika" in messages[0]
    assert "PRIVATE_" not in messages[0]
    assert "PRIVATE_" not in caplog.text
    assert f"exception_type={type(failure).__name__}" in caplog.text



def test_deferred_recovery_failure_uses_recovery_error_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def build(
        _config: AppConfig,
        *,
        defer_startup_recovery,
        **_kwargs: object,
    ) -> tuple[object, object]:
        def fail_recovery() -> None:
            raise nika_windows._StartupRecoveryInventoryError("PRIVATE_RECOVERY_CANARY")

        defer_startup_recovery(fail_recovery)
        return object(), object()

    def launch(
        _bridge: object,
        *,
        title: str,
        on_gui_started,
    ) -> None:
        assert title == f"Nika Core {config.app_version}"
        on_gui_started()

    monkeypatch.setattr(nika_windows, "build_windows_bridge", build)
    monkeypatch.setattr(nika_windows, "launch_windows_shell", launch)

    assert nika_windows.main([]) == 1
    assert len(messages) == 1
    assert "не може безпечно перевірити незавершену роботу" in messages[0]
    assert "PRIVATE_RECOVERY_CANARY" not in messages[0]


def test_shell_launch_boundary_does_not_swallow_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))

    def build(
        _config: AppConfig,
        *,
        defer_startup_recovery,
        **_kwargs: object,
    ) -> tuple[object, object]:
        defer_startup_recovery(lambda: None)
        return object(), object()

    monkeypatch.setattr(nika_windows, "build_windows_bridge", build)

    def stop_process(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(73)

    monkeypatch.setattr(nika_windows, "launch_windows_shell", stop_process)

    with pytest.raises(SystemExit) as caught:
        nika_windows.main([])
    assert caught.value.code == 73

