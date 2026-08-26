from __future__ import annotations

import json
import os
import threading

import pytest

from nika_core.speech import (
    MAX_SPEECH_TEXT_CHARS,
    SpeechError,
    SpeechErrorCode,
    SpeechRequest,
    WindowsSystemSpeechAdapter,
)


class FakeBackend:
    def __init__(self) -> None:
        self.spoken_payloads: list[dict[str, object]] = []
        self.voice_payload = json.dumps(
            [
                {
                    "voice_id": "Microsoft Test",
                    "culture": "uk-UA",
                    "gender": "Female",
                    "age": "Adult",
                    "enabled": True,
                }
            ]
        ).encode()
        self.speak_payload = b'{"voice_id":"Microsoft Test"}'

    def list_voices(self, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds > 0
        return self.voice_payload

    def speak(
        self,
        payload: bytes,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> bytes:
        assert timeout_seconds > 0
        assert cancel_event is None or not cancel_event.is_set()
        self.spoken_payloads.append(json.loads(payload.decode("utf-8")))
        return self.speak_payload


class BlockingBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def speak(
        self,
        payload: bytes,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> bytes:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release blocking speech backend")
        return super().speak(
            payload,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )


@pytest.mark.parametrize(
    "sample_request",
    [
        SpeechRequest("Привіт"),
        SpeechRequest("Hello", voice_id="Microsoft Test", rate=-3, volume=75),
    ],
)
def test_speech_request_accepts_unicode_and_bounded_settings(
    sample_request: SpeechRequest,
) -> None:
    assert sample_request.text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": ""},
        {"text": " "},
        {"text": "a" * (MAX_SPEECH_TEXT_CHARS + 1)},
        {"text": "bad\x00text"},
        {"text": "hello", "rate": -11},
        {"text": "hello", "rate": True},
        {"text": "hello", "volume": 101},
        {"text": "hello", "volume": False},
        {"text": "hello", "voice_id": ""},
        {"text": "hello", "voice_id": "bad\nvoice"},
    ],
)
def test_speech_request_rejects_invalid_input(kwargs: dict[str, object]) -> None:
    with pytest.raises(SpeechError) as error:
        SpeechRequest(**kwargs)  # type: ignore[arg-type]
    assert error.value.code is SpeechErrorCode.INVALID_REQUEST


def test_adapter_lists_installed_voices_without_mutating_host() -> None:
    backend = FakeBackend()
    adapter = WindowsSystemSpeechAdapter(backend)

    voices = adapter.list_voices()

    assert len(voices) == 1
    assert voices[0].voice_id == "Microsoft Test"
    assert voices[0].culture == "uk-UA"
    assert voices[0].enabled is True
    assert backend.spoken_payloads == []


def test_adapter_sends_unicode_payload_and_returns_non_text_receipt() -> None:
    backend = FakeBackend()
    adapter = WindowsSystemSpeechAdapter(backend)
    text = "Тест із кирилицею та пробілами"

    receipt = adapter.speak(
        SpeechRequest(text, voice_id="Microsoft Test", rate=2, volume=80)
    )

    assert backend.spoken_payloads == [
        {
            "text": text,
            "voice_id": "Microsoft Test",
            "rate": 2,
            "volume": 80,
        }
    ]
    assert receipt.engine_id == "windows-system-speech"
    assert receipt.voice_id == "Microsoft Test"
    assert receipt.character_count == len(text)
    assert not hasattr(receipt, "text")


def test_adapter_fails_closed_on_duplicate_voice_identity() -> None:
    backend = FakeBackend()
    backend.voice_payload = json.dumps(
        [
            {
                "voice_id": "Same Voice",
                "culture": "en-US",
                "gender": "Female",
                "age": "Adult",
                "enabled": True,
            },
            {
                "voice_id": "same voice",
                "culture": "en-GB",
                "gender": "Female",
                "age": "Adult",
                "enabled": True,
            },
        ]
    ).encode()
    adapter = WindowsSystemSpeechAdapter(backend)

    with pytest.raises(SpeechError) as error:
        adapter.list_voices()

    assert error.value.code is SpeechErrorCode.INVALID_ENGINE_RESPONSE


def test_adapter_fails_closed_when_selected_voice_differs() -> None:
    backend = FakeBackend()
    backend.speak_payload = b'{"voice_id":"Other Voice"}'
    adapter = WindowsSystemSpeechAdapter(backend)

    with pytest.raises(SpeechError) as error:
        adapter.speak(SpeechRequest("hello", voice_id="Microsoft Test"))

    assert error.value.code is SpeechErrorCode.INVALID_ENGINE_RESPONSE


def test_adapter_rejects_cancelled_request_before_backend_effect() -> None:
    backend = FakeBackend()
    adapter = WindowsSystemSpeechAdapter(backend)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(SpeechError) as error:
        adapter.speak(SpeechRequest("hello"), cancel_event=cancel)

    assert error.value.code is SpeechErrorCode.PROCESS_CANCELLED
    assert backend.spoken_payloads == []


def test_adapter_rejects_overlapping_speech_without_second_effect() -> None:
    backend = BlockingBackend()
    first_adapter = WindowsSystemSpeechAdapter(backend)
    second_backend = FakeBackend()
    second_adapter = WindowsSystemSpeechAdapter(second_backend)
    failures: list[BaseException] = []

    def first_speech() -> None:
        try:
            first_adapter.speak(SpeechRequest("first"))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    worker = threading.Thread(target=first_speech)
    worker.start()
    assert backend.started.wait(timeout=1)
    try:
        with pytest.raises(SpeechError) as error:
            second_adapter.speak(SpeechRequest("second"))
        assert error.value.code is SpeechErrorCode.ENGINE_BUSY
        assert second_backend.spoken_payloads == []
    finally:
        backend.release.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert failures == []
    assert len(backend.spoken_payloads) == 1


@pytest.mark.parametrize("timeout", [0, -1, 3600.1, True, "1"])
def test_adapter_rejects_invalid_timeout(timeout: object) -> None:
    backend = FakeBackend()
    adapter = WindowsSystemSpeechAdapter(backend)

    with pytest.raises(SpeechError) as error:
        adapter.list_voices(timeout_seconds=timeout)  # type: ignore[arg-type]

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST


@pytest.mark.skipif(os.name != "nt", reason="requires physical Windows System.Speech host")
def test_windows_system_speech_host_returns_parseable_voice_metadata() -> None:
    voices = WindowsSystemSpeechAdapter().list_voices(timeout_seconds=20)

    assert isinstance(voices, tuple)
    assert all(voice.voice_id.strip() for voice in voices)
