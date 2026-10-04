from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from scripts import nika_windows


class _Resource:
    def __init__(
        self,
        name: str,
        closed: list[str],
        *,
        close_interrupt: bool = False,
    ) -> None:
        self.name = name
        self.closed = closed
        self.close_interrupt = close_interrupt

    def close(self) -> None:
        self.closed.append(self.name)
        if self.close_interrupt:
            raise SystemExit("PRIVATE_CLOSE_INTERRUPTION_CANARY")


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
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
def test_partial_startup_interrupt_preserves_identity_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
    failure_point: str,
    expected_closed: list[str],
) -> None:
    closed: list[str] = []
    interruption = interruption_type("PRIVATE_STARTUP_INTERRUPTION_CANARY")

    class Backend(_Resource):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("backend", closed)

        def submit_packaged_coroutine(self, _coroutine: object) -> None:
            pytest.fail("startup must not dispatch task effects")

        def start_startup_recovery(self) -> None:
            if failure_point == "recovery":
                raise interruption

    def build_voice(*_args: object, **_kwargs: object) -> _Resource:
        if failure_point == "voice":
            raise interruption
        return _Resource("voice", closed)

    def build_model_setup(*_args: object, **_kwargs: object) -> _Resource:
        if failure_point == "model_setup":
            raise interruption
        return _Resource("model_setup", closed)

    def build_speech() -> _Resource:
        if failure_point == "speech":
            raise interruption
        return _Resource("speech", closed)

    monkeypatch.setattr(nika_windows, "DesktopBackend", Backend)
    monkeypatch.setattr(nika_windows, "build_packaged_voice", build_voice)
    monkeypatch.setattr(nika_windows, "PackagedVoiceModelSetup", build_model_setup)
    monkeypatch.setattr(nika_windows, "build_packaged_speech", build_speech)
    if failure_point == "assembly":

        def fail_assembly(_repository: object) -> None:
            raise interruption

        monkeypatch.setattr(nika_windows, "ProductProjectCommandService", fail_assembly)

    config = AppConfig(database_path=tmp_path / "Приватна папка" / "ніка.db")
    with pytest.raises(interruption_type) as captured:
        nika_windows.build_windows_session(config)

    assert captured.value is interruption
    assert closed == expected_closed


def test_cleanup_interrupt_does_not_replace_original_or_skip_other_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []
    interruption = KeyboardInterrupt("PRIVATE_ORIGINAL_INTERRUPTION_CANARY")

    class Backend(_Resource):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__("backend", closed)

        def submit_packaged_coroutine(self, _coroutine: object) -> None:
            pytest.fail("startup must not dispatch task effects")

        def start_startup_recovery(self) -> None:
            raise interruption

    monkeypatch.setattr(nika_windows, "DesktopBackend", Backend)
    monkeypatch.setattr(
        nika_windows,
        "build_packaged_voice",
        lambda *_args, **_kwargs: _Resource(
            "voice", closed, close_interrupt=True
        ),
    )
    monkeypatch.setattr(
        nika_windows,
        "PackagedVoiceModelSetup",
        lambda *_args, **_kwargs: _Resource("model_setup", closed),
    )
    monkeypatch.setattr(
        nika_windows,
        "build_packaged_speech",
        lambda: _Resource("speech", closed),
    )

    config = AppConfig(database_path=tmp_path / "Приватна папка" / "ніка.db")
    with caplog.at_level(logging.ERROR), pytest.raises(KeyboardInterrupt) as captured:
        nika_windows.build_windows_session(config)

    assert captured.value is interruption
    assert closed == ["speech", "model_setup", "voice", "backend"]
    assert "component=voice exception_type=SystemExit" in caplog.text
    assert "PRIVATE_" not in caplog.text
