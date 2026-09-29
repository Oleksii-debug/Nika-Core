from __future__ import annotations

import threading
import time

import pytest

from nika_core.speech.contracts import (
    SpeechError,
    SpeechErrorCode,
    SpeechReceipt,
    SpeechRequest,
)
from nika_core.speech.streaming import (
    MAX_STREAM_PENDING_CHARS,
    MAX_STREAM_TOTAL_CHARS,
    IncrementalSpeechStream,
    SpeechStreamState,
)


class FakeSpeechPort:
    def __init__(self) -> None:
        self.requests: list[SpeechRequest] = []
        self.voice_id = "Nika Test Voice"

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: threading.Event | None = None,
    ) -> SpeechReceipt:
        assert timeout_seconds > 0
        assert cancel_event is None or not cancel_event.is_set()
        self.requests.append(request)
        voice_id = request.voice_id or self.voice_id
        return SpeechReceipt(
            engine_id="test-engine",
            voice_id=voice_id,
            character_count=len(request.text),
            rate=request.rate,
            volume=request.volume,
        )


class BlockingSpeechPort(FakeSpeechPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: threading.Event | None = None,
    ) -> SpeechReceipt:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release speech")
        return super().speak(
            request,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )


class CancelAwareSpeechPort(FakeSpeechPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: threading.Event | None = None,
    ) -> SpeechReceipt:
        del request, timeout_seconds
        self.started.set()
        assert cancel_event is not None
        if not cancel_event.wait(timeout=2):
            raise AssertionError("test did not cancel speech")
        raise SpeechError(
            SpeechErrorCode.PROCESS_CANCELLED,
            "CANARY_RAW_TEXT_must_not_escape",
        )


class _HostileFragment(str):
    def __len__(self) -> int:
        raise AssertionError("hostile fragment behavior must not execute")


def _wait_for_request_count(port: FakeSpeechPort, count: int) -> None:
    deadline = time.monotonic() + 1
    while len(port.requests) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(port.requests) >= count


def test_stream_speaks_complete_sentence_before_finish_and_flushes_tail() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port, chunk_chars=200)

    stream.feed("Привіт, Ніко. Це хвіст")
    _wait_for_request_count(port, 1)

    assert port.requests[0].text == "Привіт, Ніко."
    assert stream.snapshot().state is SpeechStreamState.RUNNING

    stream.finish()
    assert stream.wait(1)

    assert [request.text for request in port.requests] == [
        "Привіт, Ніко.",
        "Це хвіст",
    ]
    result = stream.result()
    assert result.chunk_count == 2
    assert result.character_count == len("Привіт, Ніко.") + len("Це хвіст")
    assert not hasattr(result, "text")


def test_stream_processes_multiple_ready_sentences_without_waiting_for_more_input() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port, chunk_chars=200)

    stream.feed("Перше. Друге. Третє")
    _wait_for_request_count(port, 2)

    assert [request.text for request in port.requests[:2]] == ["Перше.", "Друге."]

    stream.finish()
    assert stream.wait(1)
    assert [request.text for request in port.requests] == ["Перше.", "Друге.", "Третє"]


def test_stream_accepts_more_fragments_while_current_speech_is_active() -> None:
    port = BlockingSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Перше.")
    assert port.started.wait(timeout=1)

    stream.feed(" Друге.")
    assert stream.snapshot().accepted_characters == len("Перше. Друге.")

    port.release.set()
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert [request.text for request in port.requests] == ["Перше.", "Друге."]


def test_stream_pins_selected_voice_after_first_chunk() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Перше.")
    _wait_for_request_count(port, 1)
    stream.feed(" Друге.")
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert port.requests[0].voice_id is None
    assert port.requests[1].voice_id == "Nika Test Voice"
    assert stream.result().voice_id == "Nika Test Voice"


def test_chunk_threshold_starts_output_without_sentence_boundary() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port, chunk_chars=5)

    stream.feed("abcdefgh")
    _wait_for_request_count(port, 1)

    assert port.requests[0].text == "abcde"

    stream.finish()
    assert stream.wait(1)
    assert [request.text for request in port.requests] == ["abcde", "fgh"]


def test_cancel_reaches_active_speak_and_prevents_later_output() -> None:
    port = CancelAwareSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Секретний текст.")
    assert port.started.wait(timeout=1)

    stream.cancel()
    assert stream.wait(1)
    assert stream.snapshot().state is SpeechStreamState.CANCELLED

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_CANCELLED
    assert "CANARY_RAW_TEXT" not in str(error.value)


def test_feed_rejects_behavioral_string_without_invoking_it() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    with pytest.raises(SpeechError) as error:
        stream.feed(_HostileFragment("text"))

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    stream.cancel()
    assert stream.wait(1)
    assert port.requests == []


def test_pending_buffer_is_bounded_while_speech_is_blocked() -> None:
    port = BlockingSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Початок.")
    assert port.started.wait(timeout=1)

    with pytest.raises(SpeechError) as error:
        stream.feed("x" * (MAX_STREAM_PENDING_CHARS + 1))

    assert error.value.code is SpeechErrorCode.ENGINE_BUSY
    port.release.set()
    stream.finish()
    assert stream.wait(1)


def test_finish_with_only_whitespace_has_no_audio_effect() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("   \n   ")
    stream.finish()
    assert stream.wait(1)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert port.requests == []


def test_result_before_completion_is_retryable_busy() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.ENGINE_BUSY
    assert error.value.retryable is True
    stream.cancel()
    assert stream.wait(1)


def test_unexpected_port_failure_is_privacy_minimized() -> None:
    class ExplodingPort(FakeSpeechPort):
        def speak(
            self,
            request: SpeechRequest,
            *,
            timeout_seconds: float = 120.0,
            cancel_event: threading.Event | None = None,
        ) -> SpeechReceipt:
            del request, timeout_seconds, cancel_event
            raise RuntimeError("CANARY_RAW_SPOKEN_TEXT")

    stream = IncrementalSpeechStream(ExplodingPort())
    stream.feed("Приватний текст.")
    stream.finish()
    assert stream.wait(1)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_FAILED
    assert "CANARY" not in str(error.value)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda receipt: object.__setattr__(receipt, "character_count", 999),
        lambda receipt: object.__setattr__(receipt, "engine_id", ""),
        lambda receipt: object.__setattr__(receipt, "voice_id", "bad\nvoice"),
        lambda receipt: object.__setattr__(receipt, "rate", True),
    ],
)
def test_invalid_port_receipt_fails_closed(mutator: object) -> None:
    class InvalidReceiptPort(FakeSpeechPort):
        def speak(
            self,
            request: SpeechRequest,
            *,
            timeout_seconds: float = 120.0,
            cancel_event: threading.Event | None = None,
        ) -> SpeechReceipt:
            receipt = super().speak(
                request,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
            mutator(receipt)  # type: ignore[operator]
            return receipt

    stream = IncrementalSpeechStream(InvalidReceiptPort())
    stream.feed("Тест.")
    stream.finish()
    assert stream.wait(1)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.INVALID_ENGINE_RESPONSE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chunk_chars": 0},
        {"chunk_chars": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
    ],
)
def test_constructor_rejects_invalid_stream_bounds(kwargs: dict[str, object]) -> None:
    with pytest.raises(SpeechError) as error:
        IncrementalSpeechStream(FakeSpeechPort(), **kwargs)  # type: ignore[arg-type]

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST


def test_feed_after_finish_fails_before_new_effect() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Готово.")
    stream.finish()

    with pytest.raises(SpeechError) as error:
        stream.feed("Запізно.")

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert stream.wait(1)


def test_wait_rejects_behavioral_or_nonfinite_timeout() -> None:
    class HostileFloat(float):
        def __float__(self) -> float:
            raise AssertionError("hostile timeout behavior must not execute")

    stream = IncrementalSpeechStream(FakeSpeechPort())

    for value in (HostileFloat(1), float("inf"), -1):
        with pytest.raises(SpeechError) as error:
            stream.wait(value)
        assert error.value.code is SpeechErrorCode.INVALID_REQUEST

    stream.cancel()
    assert stream.wait(1)


def test_total_stream_bound_fails_before_audio_effect() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    with pytest.raises(SpeechError) as error:
        stream.feed("x" * (MAX_STREAM_TOTAL_CHARS + 1))

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    stream.cancel()
    assert stream.wait(1)
    assert port.requests == []


def test_empty_feed_after_finish_is_rejected_like_other_late_input() -> None:
    stream = IncrementalSpeechStream(FakeSpeechPort())
    stream.finish()

    with pytest.raises(SpeechError) as error:
        stream.feed("")

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert stream.wait(1)


def test_cancel_after_completion_is_idempotent_and_does_not_relabel_result() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)
    stream.feed("Готово.")
    stream.finish()
    assert stream.wait(1)

    before = stream.snapshot()
    stream.cancel()
    after = stream.snapshot()

    assert before.state is SpeechStreamState.COMPLETED
    assert after.state is SpeechStreamState.COMPLETED
    assert after.cancellation_requested is False
    assert stream.result().chunk_count == 1
