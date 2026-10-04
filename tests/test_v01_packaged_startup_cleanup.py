from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from scripts import nika_windows


class _Resource:
    def __init__(
        self, name: str, closed: list[str], *, close_error: bool = False
    ) -> None:
        self.name = name
        self.closed = closed
        self.close_error = close_error

    def close(self) -> None:
        self.closed.append(self.name)
        if self.close_error:
            raise OSError("PRIVATE_CLEANUP_PATH_CANARY")


@pytest.mark.parametrize(
    ("failure_point", "expected_closed"),
    [
        ("voice", ["backend"]),
        ("model_setup", ["voice", "backend"]),
        ("speech", ["model_setup", "voice", "backend"]),
        ("recovery", ["speech", "model_setup", "voice", "backend"]),
        ("assembly", ["speech", "model_setup", "voice", "backend"]),
    ],
)
def test_partial_packaged_startup_closes_only_constructed_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_closed: list[str],
) -> None:
    closed: list[str] = []
    failure = RuntimeError("PRIVATE_STARTUP_PATH_CANARY")

    class Backend(_Resource):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("backend", closed)

        def submit_packaged_coroutine(self, _coroutine: object) -> None:
            pytest.fail("startup must not submit runtime effects")

        def start_startup_recovery(self) -> None:
            if failure_point == "recovery":
                raise failure

    def build_voice(*_args: object, **_kwargs: object) -> _Resource:
        if failure_point == "voice":
            raise failure
        return _Resource("voice", closed)

    def build_setup(*_args: object, **_kwargs: object) -> _Resource:
        if failure_point == "model_setup":
            raise failure
        return _Resource("model_setup", closed)

    def build_speech() -> _Resource:
        if failure_point == "speech":
            raise failure
        return _Resource("speech", closed)

    monkeypatch.setattr(nika_windows, "DesktopBackend", Backend)
    monkeypatch.setattr(nika_windows, "build_packaged_voice", build_voice)
    monkeypatch.setattr(nika_windows, "PackagedVoiceModelSetup", build_setup)
    monkeypatch.setattr(nika_windows, "build_packaged_speech", build_speech)
    if failure_point == "assembly":
        def fail_assembly(_repository: object) -> None:
            raise failure

        monkeypatch.setattr(nika_windows, "ProductProjectCommandService", fail_assembly)
    config = AppConfig(database_path=tmp_path / "Приватні дані" / "ніка.db")
    expected_error = (
        nika_windows._StartupRecoveryInventoryError
        if failure_point == "recovery"
        else RuntimeError
    )

    with pytest.raises(expected_error) as captured:
        nika_windows.build_windows_session(config)

    assert closed == expected_closed
    if failure_point == "recovery":
        assert captured.value.__cause__ is failure
    else:
        assert captured.value is failure


def test_cleanup_failure_preserves_original_error_and_sanitizes_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []
    failure = RuntimeError("PRIVATE_SPEECH_SETUP_CANARY")

    class Backend(_Resource):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("backend", closed)

        def submit_packaged_coroutine(self, _coroutine: object) -> None:
            pytest.fail("startup must not submit runtime effects")

    monkeypatch.setattr(nika_windows, "DesktopBackend", Backend)
    monkeypatch.setattr(
        nika_windows,
        "build_packaged_voice",
        lambda *_args, **_kwargs: _Resource(
            "voice", closed, close_error=True
        ),
    )
    monkeypatch.setattr(
        nika_windows,
        "PackagedVoiceModelSetup",
        lambda *_args, **_kwargs: _Resource("model_setup", closed),
    )

    def fail_speech() -> None:
        raise failure

    monkeypatch.setattr(nika_windows, "build_packaged_speech", fail_speech)
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "ніка.db")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as captured:
        nika_windows.build_windows_session(config)

    assert captured.value is failure
    assert closed == ["model_setup", "voice", "backend"]
    assert "component=voice exception_type=OSError" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_successful_session_closes_in_reverse_order_once() -> None:
    closed: list[str] = []
    session = nika_windows.WindowsBridgeSession(
        bridge=object(),
        products=object(),
        backend=_Resource("backend", closed),
        voice=_Resource("voice", closed),
        voice_model_setup=_Resource("model_setup", closed),
        speech=_Resource("speech", closed),
    )

    session.close()
    session.close()
    assert closed == ["speech", "model_setup", "voice", "backend"]


def test_failed_startup_cleanup_continues_after_multiple_close_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []
    with caplog.at_level(logging.ERROR):
        nika_windows._close_failed_startup_resources(
            _Resource("backend", closed),
            voice=_Resource("voice", closed),
            voice_model_setup=_Resource("model_setup", closed, close_error=True),
            speech=_Resource("speech", closed, close_error=True),
        )

    assert closed == ["speech", "model_setup", "voice", "backend"]
    assert "component=speech exception_type=OSError" in caplog.text
    assert "component=voice_model_setup exception_type=OSError" in caplog.text
    assert "PRIVATE_" not in caplog.text
