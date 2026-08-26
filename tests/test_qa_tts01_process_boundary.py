from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import nika_core.speech.windows as speech_windows
from nika_core.speech import SpeechError, SpeechErrorCode, WindowsSystemSpeechAdapter


class _RecordingBackend:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_voices(self, *, timeout_seconds: float) -> bytes:
        self.list_calls += 1
        return b"[]"

    def speak(
        self,
        payload: bytes,
        *,
        timeout_seconds: float,
        cancel_event: Any,
    ) -> bytes:
        raise AssertionError("speak is not part of this oracle")


@dataclass
class _LiveProcess:
    pid: int = 424242

    def poll(self) -> None:
        return None


def _capture_cleanup_call(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    captured: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def fake_run(argv: tuple[str, ...], **kwargs: Any) -> object:
        captured.append((tuple(str(part) for part in argv), dict(kwargs)))
        return object()

    monkeypatch.setattr(speech_windows.subprocess, "run", fake_run)
    speech_windows._terminate_process_tree(_LiveProcess())
    assert len(captured) == 1
    return captured[0]


def test_adapter_rejects_nan_timeout_before_backend_effect() -> None:
    backend = _RecordingBackend()
    adapter = WindowsSystemSpeechAdapter(backend)

    with pytest.raises(SpeechError) as error:
        adapter.list_voices(timeout_seconds=float("nan"))

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert backend.list_calls == 0


def test_process_cleanup_does_not_resolve_taskkill_from_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv, _kwargs = _capture_cleanup_call(monkeypatch)

    executable = argv[0].replace("\\", "/").casefold()
    assert executable != "taskkill"
    assert executable.endswith("/system32/taskkill.exe")


def test_process_cleanup_has_its_own_bounded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _argv, kwargs = _capture_cleanup_call(monkeypatch)

    timeout = kwargs.get("timeout")
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
    assert 0 < timeout <= 30
