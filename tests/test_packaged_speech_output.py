from __future__ import annotations

import time
from threading import Event

import pytest

import nika_core.ui.packaged_speech as packaged_speech
from nika_core.config import AppConfig
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.speech import (
    SpeechError,
    SpeechErrorCode,
    SpeechReceipt,
    SpeechRequest,
)
from nika_core.ui.packaged_speech import PackagedSpeechFeature
from scripts import nika_windows


class _FakeSpeechPort:
    def __init__(self) -> None:
        self.requests: list[SpeechRequest] = []

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt:
        assert timeout_seconds > 0
        assert cancel_event is None or not cancel_event.is_set()
        self.requests.append(request)
        return SpeechReceipt(
            engine_id="test-local-speech",
            voice_id="Test Voice",
            character_count=len(request.text),
            rate=request.rate,
            volume=request.volume,
        )


class _BlockingSpeechPort(_FakeSpeechPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt:
        del timeout_seconds
        assert cancel_event is not None
        self.requests.append(request)
        self.started.set()
        if not cancel_event.wait(timeout=2):
            raise AssertionError("test did not cancel blocking packaged speech")
        raise SpeechError(
            SpeechErrorCode.PROCESS_CANCELLED,
            "speech output was cancelled",
        )


class _StubbornCancellingSpeechPort(_FakeSpeechPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt:
        del timeout_seconds
        assert cancel_event is not None
        self.requests.append(request)
        self.started.set()
        if not cancel_event.wait(timeout=2):
            raise AssertionError("test did not request packaged speech cancellation")
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release cancelling packaged speech")
        raise SpeechError(
            SpeechErrorCode.PROCESS_CANCELLED,
            "speech output was cancelled",
        )


class _HostileText(str):
    def strip(self, chars: str | None = None) -> str:
        del chars
        raise AssertionError("behavioral string methods must not execute")


def _wait_for_status(
    feature: PackagedSpeechFeature,
    expected: str,
    *,
    timeout: float = 1.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = feature.snapshot()
        if snapshot["status"] == expected:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(
        f"packaged speech did not reach {expected!r}: {feature.snapshot()!r}"
    )


def test_packaged_speech_runs_explicit_bounded_text_without_projecting_text() -> None:
    port = _FakeSpeechPort()
    feature = PackagedSpeechFeature(output=port)

    result = feature.speak({"text": "Привіт, це локальне озвучення."})
    snapshot = _wait_for_status(feature, "completed")

    assert result.status == "completed"
    assert result.focus_id == "speech-cancel"
    assert [item.text for item in port.requests] == ["Привіт, це локальне озвучення."]
    assert snapshot["schema"] == "nika.packaged-speech-state:v1"
    assert snapshot["available"] is True
    assert snapshot["active"] is False
    assert snapshot["generation"] == 1
    assert snapshot["accepted_characters"] == len("Привіт, це локальне озвучення.")
    assert snapshot["spoken_characters"] == len("Привіт, це локальне озвучення.")
    assert "text" not in snapshot
    assert "Привіт" not in repr(snapshot)


def test_packaged_speech_rejects_parallel_start_and_supports_explicit_cancel() -> None:
    port = _BlockingSpeechPort()
    feature = PackagedSpeechFeature(output=port)

    first = feature.speak({"text": "Довге озвучення."})
    assert first.status == "completed"
    assert port.started.wait(timeout=1)

    second = feature.speak({"text": "Не має стартувати паралельно."})
    assert second.status == "rejected"
    assert second.focus_id == "speech-cancel"
    assert len(port.requests) == 1

    cancelled = feature.cancel({})
    assert cancelled.status == "completed"
    snapshot = _wait_for_status(feature, "cancelled")
    assert snapshot["active"] is False
    assert snapshot["generation"] == 1


def test_packaged_speech_exposes_cancelling_and_deduplicates_cancel() -> None:
    port = _StubbornCancellingSpeechPort()
    feature = PackagedSpeechFeature(output=port)
    feature.speak({"text": "Контрольоване скасування."})
    assert port.started.wait(timeout=1)

    first_cancel = feature.cancel({})
    snapshot = _wait_for_status(feature, "cancelling")
    second_cancel = feature.cancel({})

    assert first_cancel.status == "completed"
    assert snapshot["active"] is True
    assert snapshot["message"] == "Скасування озвучення виконується."
    assert second_cancel.status == "completed"
    assert second_cancel.message == "Скасування озвучення вже запитано."

    port.release.set()
    _wait_for_status(feature, "cancelled")


def test_packaged_speech_close_cancels_active_work_and_is_idempotent() -> None:
    port = _BlockingSpeechPort()
    feature = PackagedSpeechFeature(output=port)
    feature.speak({"text": "Скасувати під час закриття."})
    assert port.started.wait(timeout=1)

    feature.close()
    feature.close()

    assert feature.snapshot()["status"] == "cancelled"
    after_close = feature.speak({"text": "Не запускати після закриття."})
    assert after_close.status == "rejected"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "ok", "extra": True},
        {"text": 7},
        {"text": _HostileText("hostile")},
    ],
)
def test_packaged_speech_rejects_noncanonical_action_payload(payload: dict[str, object]) -> None:
    feature = PackagedSpeechFeature(output=_FakeSpeechPort())

    with pytest.raises((TypeError, ValueError)):
        feature.speak(payload)  # type: ignore[arg-type]


def test_unavailable_packaged_speech_is_bounded_and_effect_free() -> None:
    feature = PackagedSpeechFeature(
        output=None,
        unavailable_message="Локальне озвучення недоступне.",
    )

    snapshot = feature.snapshot()
    result = feature.speak({"text": "Не озвучувати."})

    assert snapshot == {
        "schema": "nika.packaged-speech-state:v1",
        "available": False,
        "status": "unavailable",
        "generation": 0,
        "active": False,
        "message": "Локальне озвучення недоступне.",
        "accepted_characters": 0,
        "spoken_characters": 0,
        "chunk_count": 0,
        "pending_characters": 0,
    }
    assert result.status == "rejected"


def test_builder_fails_closed_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaged_speech.sys, "platform", "linux")

    feature = packaged_speech.build_packaged_speech()

    assert feature.available is False
    assert feature.snapshot()["status"] == "unavailable"


def test_builder_fails_closed_when_windows_speech_host_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packaged_speech.sys, "platform", "win32")

    def unavailable() -> object:
        raise SpeechError(
            SpeechErrorCode.ENGINE_UNAVAILABLE,
            "sensitive operating-system diagnostic",
        )

    monkeypatch.setattr(packaged_speech, "WindowsSystemSpeechAdapter", unavailable)

    feature = packaged_speech.build_packaged_speech()

    snapshot = feature.snapshot()
    assert feature.available is False
    assert "sensitive" not in str(snapshot)
    assert snapshot["message"] == (
        "Локальне озвучення Windows недоступне. "
        "Перевірте системний компонент Windows System.Speech."
    )


def test_packaged_speech_actions_are_registered() -> None:
    actions = {item.action_id: item for item in build_default_action_registry().all()}

    assert actions["speech.start"].label == "Озвучити текст"
    assert actions["speech.cancel"].label == "Скасувати озвучення"


def test_packaged_speech_ui_preserves_single_live_region() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    script = (root / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")

    assert '<h2 id="speech-heading" tabindex="-1">Голос Nika</h2>' in html
    assert 'id="speech-text"' in html
    assert 'maxlength="20000"' in html
    assert 'data-action-id="speech.start"' in html
    assert 'data-action-id="speech.cancel"' in html
    assert '<p id="speech-status">' in html
    assert html.count('role="status"') == 1
    assert html.count('aria-live="polite"') == 1
    assert 'state.speech ?? null' in script
    assert 'if (actionId === "speech.start") payload.text = speechText?.value ?? "";' in script
    assert "function renderSpeech(snapshot)" in script

def test_current_windows_bridge_wires_speech_state_actions_and_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _FakeSpeechPort()
    feature = PackagedSpeechFeature(output=port)
    monkeypatch.setattr(nika_windows, "build_packaged_speech", lambda: feature)
    cleanup_callbacks: list[object] = []

    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=(tmp_path / "nika.db").resolve()),
        start_startup_recovery=False,
        register_cleanup=cleanup_callbacks.append,
    )

    state = bridge.get_state()
    assert state["ok"] is True
    speech_state = state["state"]["speech"]
    assert speech_state["schema"] == "nika.packaged-speech-state:v1"
    assert speech_state["status"] == "idle"
    assert "text" not in speech_state
    assert len(cleanup_callbacks) == 3
    assert cleanup_callbacks[-1] == feature.close

    started = bridge.dispatch(
        {
            "request_id": "packaged-speech-current-terminal",
            "action_id": "speech.start",
            "payload": {"text": "Явний локальний тест озвучення."},
        }
    )
    assert started["request_id"] == "packaged-speech-current-terminal"
    assert started["status"] == "completed"
    snapshot = _wait_for_status(feature, "completed")
    assert snapshot["spoken_characters"] == len("Явний локальний тест озвучення.")
    assert [request.text for request in port.requests] == [
        "Явний локальний тест озвучення."
    ]

    for cleanup in reversed(cleanup_callbacks):
        assert callable(cleanup)
        cleanup()
    rejected = bridge.dispatch(
        {
            "request_id": "packaged-speech-after-close",
            "action_id": "speech.start",
            "payload": {"text": "Не запускати після cleanup."},
        }
    )
    assert rejected["status"] == "rejected"

