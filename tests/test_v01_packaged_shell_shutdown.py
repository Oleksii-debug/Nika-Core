from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from scripts import nika_windows


@pytest.mark.parametrize(
    ("launch_fails", "close_fails"),
    [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_packaged_shell_and_shutdown_fail_safely_without_private_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    launch_fails: bool,
    close_fails: bool,
) -> None:
    calls: list[str] = []
    messages: list[str] = []
    config = AppConfig(database_path=tmp_path / "Приватна папка" / "ніка.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))

    class Session:
        bridge = object()

        def close(self) -> None:
            calls.append("close")
            if close_fails:
                raise OSError("PRIVATE_SHUTDOWN_PATH_CANARY")

    session = Session()
    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _config: session)
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def launch(bridge: object, *, title: str) -> None:
        assert bridge is session.bridge
        assert title == f"Nika Core {config.app_version}"
        calls.append("launch")
        if launch_fails:
            raise RuntimeError("PRIVATE_SHELL_ERROR_CANARY")

    monkeypatch.setattr(nika_windows, "launch_windows_shell", launch)
    with caplog.at_level(logging.ERROR):
        result = nika_windows.main([])

    assert result == (1 if launch_fails or close_fails else 0)
    assert calls == ["launch", "close"]
    if launch_fails or close_fails:
        assert len(messages) == 1
        assert "Збережіть папку даних" in messages[0]
    else:
        assert messages == []
    assert "PRIVATE_" not in caplog.text
    assert all("PRIVATE_" not in message for message in messages)
    assert ("Packaged shell failed" in caplog.text) is launch_fails
    assert ("Packaged shutdown failed" in caplog.text) is close_fails


def test_packaged_keyboard_interrupt_still_closes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(database_path=tmp_path / "ніка.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    closed: list[bool] = []

    class Session:
        bridge = object()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _config: Session())

    def interrupt(_bridge: object, *, title: str) -> None:
        del title
        raise KeyboardInterrupt

    monkeypatch.setattr(nika_windows, "launch_windows_shell", interrupt)
    with pytest.raises(KeyboardInterrupt):
        nika_windows.main([])

    assert closed == [True]


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt("PRIVATE_INTERRUPT_CANARY"), SystemExit("PRIVATE_EXIT_CANARY")],
)
def test_packaged_interrupt_is_not_masked_by_shutdown_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    interruption: BaseException,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "ніка.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    closed: list[str] = []
    messages: list[str] = []
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    class Session:
        bridge = object()

        def close(self) -> None:
            closed.append("close")
            raise OSError("PRIVATE_CLOSE_CANARY")

    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _config: Session())

    def interrupt(_bridge: object, *, title: str) -> None:
        del title
        raise interruption

    monkeypatch.setattr(nika_windows, "launch_windows_shell", interrupt)
    with caplog.at_level(logging.ERROR), pytest.raises(type(interruption)) as captured:
        nika_windows.main([])

    assert captured.value is interruption
    assert closed == ["close"]
    assert messages == []
    assert "Packaged shutdown failed: exception_type=OSError" in caplog.text
    assert "PRIVATE_" not in caplog.text
