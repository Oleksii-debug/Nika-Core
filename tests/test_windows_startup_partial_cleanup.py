from __future__ import annotations

from unittest.mock import Mock

import pytest

from nika_core.config import AppConfig
from nika_core.ui import startup_error
from scripts import nika_windows


@pytest.mark.parametrize("failed_stage", ["voice", "setup", "speech", "recovery"])
def test_partial_windows_startup_closes_only_initialized_components(
    monkeypatch, tmp_path, failed_stage: str
) -> None:
    config = AppConfig(database_path=tmp_path / "дані з пробілами" / "nika.db")
    closed: list[str] = []
    components = {name: Mock() for name in ("backend", "voice", "setup", "speech")}
    for name, component in components.items():
        component.close.side_effect = lambda name=name: closed.append(name)

    def make(stage: str, value):
        if stage == failed_stage:
            raise OSError("private-credential-and-path")
        return value

    monkeypatch.setattr(
        nika_windows, "V01PackagedThreeAgentRuntime", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        nika_windows, "DesktopBackend", lambda **_kwargs: components["backend"]
    )
    monkeypatch.setattr(
        nika_windows, "build_packaged_voice",
        lambda *_args, **_kwargs: make("voice", components["voice"]),
    )
    monkeypatch.setattr(
        nika_windows, "PackagedVoiceModelSetup",
        lambda *_args, **_kwargs: make("setup", components["setup"]),
    )
    monkeypatch.setattr(
        nika_windows, "build_packaged_speech",
        lambda: make("speech", components["speech"]),
    )
    if failed_stage == "recovery":
        components["backend"].start_startup_recovery.side_effect = OSError(
            "private-recovery-path"
        )

    if failed_stage == "recovery":
        with pytest.raises(nika_windows._StartupRecoveryInventoryError) as exc:
            nika_windows.build_windows_session(config)
        assert "private-" not in str(exc.value)
    else:
        with pytest.raises(OSError, match="private-credential-and-path"):
            nika_windows.build_windows_session(config)

    expected = {
        "voice": ["backend"],
        "setup": ["voice", "backend"],
        "speech": ["setup", "voice", "backend"],
        "recovery": ["speech", "setup", "voice", "backend"],
    }
    assert closed == expected[failed_stage]
    for name, component in components.items():
        if name in closed:
            component.close.assert_called_once_with()
        else:
            component.close.assert_not_called()


def test_cleanup_failure_does_not_hide_recovery_error(
    monkeypatch, tmp_path, caplog
) -> None:
    config = AppConfig(database_path=tmp_path / "ніка" / "nika.db")
    closed: list[str] = []
    components = {name: Mock() for name in ("backend", "voice", "setup", "speech")}
    for name, component in components.items():
        def fail_close(name=name):
            closed.append(name)
            raise RuntimeError("private-close-secret")
        component.close.side_effect = fail_close
    components["backend"].start_startup_recovery.side_effect = ValueError(
        "private-inventory-secret"
    )
    monkeypatch.setattr(
        nika_windows, "V01PackagedThreeAgentRuntime", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        nika_windows, "DesktopBackend", lambda **_kwargs: components["backend"]
    )
    monkeypatch.setattr(
        nika_windows, "build_packaged_voice",
        lambda *_args, **_kwargs: components["voice"],
    )
    monkeypatch.setattr(
        nika_windows, "PackagedVoiceModelSetup",
        lambda *_args, **_kwargs: components["setup"],
    )
    monkeypatch.setattr(
        nika_windows, "build_packaged_speech", lambda: components["speech"]
    )

    with pytest.raises(nika_windows._StartupRecoveryInventoryError) as exc:
        nika_windows.build_windows_session(config)
    assert "private-" not in str(exc.value)
    assert closed == ["speech", "setup", "voice", "backend"]
    assert "private-" not in caplog.text
    assert caplog.text.count("Windows startup cleanup failed") == 4


def test_partial_factory_failure_surfaces_safe_native_startup_error(
    monkeypatch, tmp_path
) -> None:
    config = AppConfig(database_path=tmp_path / "ніка" / "nika.db")
    backend = Mock()
    messages: list[str] = []
    monkeypatch.setattr(startup_error, "show_recovery_error", messages.append)
    monkeypatch.setattr(
        nika_windows.AppConfig, "from_environment", staticmethod(lambda: config)
    )
    monkeypatch.setattr(
        nika_windows, "V01PackagedThreeAgentRuntime", lambda **_kwargs: object()
    )
    monkeypatch.setattr(nika_windows, "DesktopBackend", lambda **_kwargs: backend)
    monkeypatch.setattr(
        nika_windows, "build_packaged_voice",
        Mock(side_effect=OSError("private-model-path")),
    )
    shell = Mock()
    monkeypatch.setattr(nika_windows, "launch_windows_shell", shell)

    assert nika_windows.main([]) == 1
    backend.close.assert_called_once_with()
    shell.assert_not_called()
    assert len(messages) == 1
    assert "локальні дані" in messages[0]
    assert "private-" not in messages[0]
