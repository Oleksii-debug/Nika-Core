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
@pytest.mark.parametrize("cleanup_error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_packaged_interrupt_is_not_masked_by_shutdown_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    interruption: BaseException,
    cleanup_error_type: type[BaseException],
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
            raise cleanup_error_type("PRIVATE_CLOSE_CANARY")

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
    assert (
        f"Packaged shutdown failed: exception_type={cleanup_error_type.__name__}"
        in caplog.text
    )
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("cleanup_error_type", [KeyboardInterrupt, SystemExit])
def test_shell_failure_reports_safely_even_if_shutdown_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_error_type: type[BaseException],
) -> None:
    config = AppConfig(database_path=tmp_path / "Приватні дані" / "ніка.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    calls: list[str] = []
    messages: list[str] = []

    class Session:
        bridge = object()

        def close(self) -> None:
            calls.append("close")
            raise cleanup_error_type("PRIVATE_SHUTDOWN_CANARY")

    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _config: Session())
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def fail_shell(_bridge: object, *, title: str) -> None:
        del title
        calls.append("shell")
        raise RuntimeError("PRIVATE_SHELL_CANARY")

    monkeypatch.setattr(nika_windows, "launch_windows_shell", fail_shell)
    with caplog.at_level(logging.ERROR):
        result = nika_windows.main([])

    assert result == 1
    assert calls == ["shell", "close"]
    assert len(messages) == 1
    assert "Збережіть папку даних" in messages[0]
    assert "Packaged shell failed: exception_type=RuntimeError" in caplog.text
    assert (
        f"Packaged shutdown failed: exception_type={cleanup_error_type.__name__}"
        in caplog.text
    )
    assert "PRIVATE_" not in caplog.text
    assert "PRIVATE_" not in messages[0]


@pytest.mark.parametrize("cleanup_error_type", [KeyboardInterrupt, SystemExit])
def test_successful_shell_preserves_real_shutdown_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error_type: type[BaseException],
) -> None:
    config = AppConfig(database_path=tmp_path / "ніка.db")
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(lambda _cls: config))
    interruption = cleanup_error_type("PRIVATE_SHUTDOWN_CANARY")
    calls: list[str] = []
    messages: list[str] = []

    class Session:
        bridge = object()

        def close(self) -> None:
            calls.append("close")
            raise interruption

    monkeypatch.setattr(nika_windows, "build_windows_session", lambda _config: Session())
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", messages.append)

    def launch(_bridge: object, *, title: str) -> None:
        del title
        calls.append("shell")

    monkeypatch.setattr(nika_windows, "launch_windows_shell", launch)
    with pytest.raises(cleanup_error_type) as captured:
        nika_windows.main([])

    assert captured.value is interruption
    assert calls == ["shell", "close"]
    assert messages == []


@pytest.mark.parametrize(
    ("first_failure", "second_failure"),
    [
        ("speech", "voice_model_setup"),
        ("voice_model_setup", "backend"),
        ("voice", "backend"),
    ],
)
def test_session_teardown_preserves_first_error_and_logs_secondary_type_only(
    caplog: pytest.LogCaptureFixture,
    first_failure: str,
    second_failure: str,
) -> None:
    closed: list[str] = []
    first_error = OSError("PRIVATE_FIRST_SHUTDOWN_ERROR")
    second_error = SystemExit("PRIVATE_SECOND_SHUTDOWN_ERROR")

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)
            if self.name == first_failure:
                raise first_error
            if self.name == second_failure:
                raise second_error

    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=Resource("backend"),
        voice=Resource("voice"),
        voice_model_setup=Resource("voice_model_setup"),
        speech=Resource("speech"),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(OSError) as captured:
        session.close()

    assert captured.value is first_error
    assert closed == ["speech", "voice_model_setup", "voice", "backend"]
    assert (
        f"Packaged shutdown cleanup failed: component={second_failure} "
        "exception_type=SystemExit"
    ) in caplog.text
    assert "PRIVATE_" not in caplog.text
    session.close()
    assert closed == ["speech", "voice_model_setup", "voice", "backend"]


@pytest.mark.parametrize(
    "failure_component", ["speech", "voice_model_setup", "voice", "backend"]
)
def test_session_teardown_preserves_single_interrupt_and_attempts_all_resources(
    failure_component: str,
) -> None:
    closed: list[str] = []
    interruption = KeyboardInterrupt("PRIVATE_SHUTDOWN_INTERRUPT")

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)
            if self.name == failure_component:
                raise interruption

    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=Resource("backend"),
        voice=Resource("voice"),
        voice_model_setup=Resource("voice_model_setup"),
        speech=Resource("speech"),
    )
    with pytest.raises(KeyboardInterrupt) as captured:
        session.close()

    assert captured.value is interruption
    assert closed == ["speech", "voice_model_setup", "voice", "backend"]
    session.close()
    assert closed == ["speech", "voice_model_setup", "voice", "backend"]
