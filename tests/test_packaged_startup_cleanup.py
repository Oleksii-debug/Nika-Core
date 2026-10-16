from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.ui import startup_error
from scripts import nika_windows


def test_packaged_cleanup_runs_reverse_order_continues_and_redacts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    def first() -> None:
        events.append("first")

    def failing() -> None:
        events.append("failing")
        raise RuntimeError("PRIVATE CLEANUP PATH C:\\Users\\owner\\secret")

    def third() -> None:
        events.append("third")

    callbacks = [first, failing, third]

    with caplog.at_level(logging.ERROR):
        nika_windows._cleanup_packaged_resources(callbacks)

    assert events == ["third", "failing", "first"]
    assert callbacks == []
    assert "RuntimeError" in caplog.text
    assert "PRIVATE CLEANUP" not in caplog.text
    assert "secret" not in caplog.text


def test_main_cleans_partial_startup_resources_before_returning_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = AppConfig(database_path=(tmp_path / "nika.db").resolve())
    events: list[str] = []
    public_errors: list[str] = []

    monkeypatch.setattr(
        nika_windows.AppConfig,
        "from_environment",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(nika_windows, "preflight_windows_shell", lambda: None)
    monkeypatch.setattr(startup_error, "show_recovery_error", public_errors.append)

    def fail_build(
        _config: AppConfig,
        *,
        register_cleanup=None,
        **_kwargs: object,
    ) -> object:
        assert register_cleanup is not None
        register_cleanup(lambda: events.append("backend"))
        register_cleanup(lambda: events.append("voice"))
        raise RuntimeError("PRIVATE CONSTRUCTOR PATH C:\\Users\\owner\\model")

    monkeypatch.setattr(nika_windows, "build_windows_bridge", fail_build)
    monkeypatch.setattr(
        nika_windows,
        "launch_windows_shell",
        lambda *_args, **_kwargs: pytest.fail("shell must not launch after build failure"),
    )

    with caplog.at_level(logging.ERROR):
        result = nika_windows.main([])

    assert result == 1
    assert events == ["voice", "backend"]
    assert public_errors == [
        "Не вдалося відкрити дані або підготувати запуск Nika. "
        "Перевірте доступність папки даних; наявну базу не видаляйте."
    ]
    assert "RuntimeError" in caplog.text
    assert "PRIVATE CONSTRUCTOR" not in caplog.text
    assert "owner" not in caplog.text


def test_main_cleans_partial_resources_on_recovery_inventory_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=(tmp_path / "nika.db").resolve())
    events: list[str] = []
    public_errors: list[str] = []

    monkeypatch.setattr(
        nika_windows.AppConfig,
        "from_environment",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(nika_windows, "preflight_windows_shell", lambda: None)
    monkeypatch.setattr(startup_error, "show_recovery_error", public_errors.append)

    def fail_build(
        _config: AppConfig,
        *,
        register_cleanup=None,
        **_kwargs: object,
    ) -> object:
        assert register_cleanup is not None
        register_cleanup(lambda: events.append("desktop"))
        register_cleanup(lambda: events.append("voice-model"))
        raise nika_windows._StartupRecoveryInventoryError("private recovery diagnostic")

    monkeypatch.setattr(nika_windows, "build_windows_bridge", fail_build)

    result = nika_windows.main([])

    assert result == 1
    assert events == ["voice-model", "desktop"]
    assert public_errors == [
        "Nika не може безпечно перевірити незавершену роботу після перезапуску. "
        "Запуск зупинено без автоматичного повторення дій."
    ]
