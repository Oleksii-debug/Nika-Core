from __future__ import annotations

import threading
import time

import pytest

import nika_core.speech.streaming as speech_streaming
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
        del timeout_seconds, cancel_event
        self.requests.append(request)
        voice_id = request.voice_id or self.voice_id
        return SpeechReceipt(
            engine_id="test-engine",
            voice_id=voice_id,
            character_count=len(request.text),
            rate=request.rate,
            volume=request.volume,
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


def test_worker_start_failure_is_reported_as_bounded_speech_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingThread:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def start(self) -> None:
            raise RuntimeError("CANARY_THREAD_DIAGNOSTIC")

    monkeypatch.setattr(speech_streaming, "Thread", FailingThread)

    with pytest.raises(SpeechError) as error:
        IncrementalSpeechStream(FakeSpeechPort())

    assert error.value.code is SpeechErrorCode.PROCESS_FAILED
    assert "CANARY" not in str(error.value)


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

    stream.feed("Перше. ")
    assert port.started.wait(timeout=1)

    stream.feed("Друге. ")
    assert stream.snapshot().accepted_characters == len("Перше. Друге. ")

    port.release.set()
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert [request.text for request in port.requests] == ["Перше.", "Друге."]


def test_stream_pins_selected_voice_after_first_chunk() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Перше. ")
    _wait_for_request_count(port, 1)
    stream.feed("Друге. ")
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert port.requests[0].voice_id is None
    assert port.requests[1].voice_id == "Nika Test Voice"
    assert stream.result().voice_id == "Nika Test Voice"


def test_first_explicit_voice_receipt_accepts_canonical_engine_casing() -> None:
    class CanonicalizingPort(FakeSpeechPort):
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
            return SpeechReceipt(
                engine_id="test-engine",
                voice_id="Nika Test Voice",
                character_count=len(request.text),
                rate=request.rate,
                volume=request.volume,
            )

    port = CanonicalizingPort()
    stream = IncrementalSpeechStream(port, voice_id="nika test voice")
    stream.feed("Перше. Друге. ")
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert port.requests[0].voice_id == "nika test voice"
    assert port.requests[1].voice_id == "Nika Test Voice"
    assert stream.result().voice_id == "Nika Test Voice"


def test_sentence_boundary_waits_for_disambiguating_following_fragment() -> None:
    assert speech_streaming._first_sentence_boundary("Версія 3.", limit=200) is None
    text = "Версія 3.14 працює. "
    boundary = speech_streaming._first_sentence_boundary(text, limit=200)
    assert boundary == len(text)


def test_sentence_boundary_accepts_closing_quote_before_whitespace() -> None:
    text = 'Вона сказала: "Готово!" Далі'
    boundary = speech_streaming._first_sentence_boundary(text, limit=200)

    assert boundary == len('Вона сказала: "Готово!" ')


def test_sentence_boundary_never_exceeds_chunk_limit() -> None:
    text = 'abcd."' + ('"' * 50) + " tail"

    assert speech_streaming._first_sentence_boundary(text, limit=5) is None

    chunk, _remaining, consumed = speech_streaming._pop_ready_chunk(
        text,
        chunk_chars=5,
        flush=False,
    )
    assert chunk == "abcd."
    assert consumed == 5


def test_split_decimal_is_spoken_as_one_sentence() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port, chunk_chars=200)

    stream.feed("Версія 3.")
    time.sleep(0.03)
    assert port.requests == []

    stream.feed("14 працює. ")
    _wait_for_request_count(port, 1)

    assert port.requests[0].text == "Версія 3.14 працює."
    stream.finish()
    assert stream.wait(1)
    assert stream.result().chunk_count == 1


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

    stream.feed("Секретний текст. ")
    assert port.started.wait(timeout=1)

    stream.cancel()
    assert stream.wait(1)
    assert stream.snapshot().state is SpeechStreamState.CANCELLED

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_CANCELLED
    assert "CANARY_RAW_TEXT" not in str(error.value)


def test_successful_chunk_is_accounted_before_late_cancellation_stops_stream() -> None:
    port = BlockingSpeechPort()
    text = "Вже озвучено."
    stream = IncrementalSpeechStream(port)
    stream.feed(text)
    stream.finish()
    assert port.started.wait(timeout=1)

    stream.cancel()
    port.release.set()
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.CANCELLED
    assert snapshot.chunk_count == 1
    assert snapshot.spoken_characters == len(text)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_CANCELLED


def test_cancel_signal_does_not_mask_non_cancel_speech_failure() -> None:
    class FailingOnCancelPort(FakeSpeechPort):
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
            assert cancel_event.wait(timeout=2)
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "CANARY_ENGINE_FAILURE",
            )

    port = FailingOnCancelPort()
    stream = IncrementalSpeechStream(port)
    stream.feed("Помилка. ")
    assert port.started.wait(timeout=1)
    stream.cancel()
    assert stream.wait(1)

    assert stream.snapshot().state is SpeechStreamState.FAILED
    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_FAILED
    assert "CANARY" not in str(error.value)


def test_output_port_cannot_forge_cancellation_by_setting_signal() -> None:
    class ForgingPort(FakeSpeechPort):
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
            assert cancel_event is not None
            cancel_event.set()
            return receipt

    port = ForgingPort()
    stream = IncrementalSpeechStream(port, chunk_chars=200)
    stream.feed("Перше. Друге. ")
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.COMPLETED
    assert snapshot.cancellation_requested is False
    assert stream.result().chunk_count == 2


def test_output_port_cannot_clear_user_cancellation_authority() -> None:
    class ClearingPort(FakeSpeechPort):
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
            del timeout_seconds
            self.started.set()
            assert cancel_event is not None
            assert cancel_event.wait(timeout=2)
            cancel_event.clear()
            self.requests.append(request)
            return SpeechReceipt(
                engine_id="test-engine",
                voice_id=request.voice_id or self.voice_id,
                character_count=len(request.text),
                rate=request.rate,
                volume=request.volume,
            )

    port = ClearingPort()
    stream = IncrementalSpeechStream(port)
    stream.feed("Вже озвучено. ")
    assert port.started.wait(timeout=1)

    stream.cancel()
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.CANCELLED
    assert snapshot.cancellation_requested is True
    assert snapshot.chunk_count == 1


def test_unsolicited_port_cancel_error_is_a_stream_failure() -> None:
    class UnsolicitedCancelPort(FakeSpeechPort):
        def speak(
            self,
            request: SpeechRequest,
            *,
            timeout_seconds: float = 120.0,
            cancel_event: threading.Event | None = None,
        ) -> SpeechReceipt:
            del request, timeout_seconds, cancel_event
            raise SpeechError(
                SpeechErrorCode.PROCESS_CANCELLED,
                "CANARY_UNSOLICITED_CANCEL",
            )

    stream = IncrementalSpeechStream(UnsolicitedCancelPort())
    stream.feed("Текст. ")
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.FAILED
    assert snapshot.cancellation_requested is False
    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.PROCESS_FAILED
    assert "CANARY" not in str(error.value)


def test_feed_rejects_behavioral_string_without_invoking_it() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    with pytest.raises(SpeechError) as error:
        stream.feed(_HostileFragment("text"))

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    stream.cancel()
    assert stream.wait(1)
    assert port.requests == []


def test_feed_rejects_invalid_utf8_without_poisoning_live_stream() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Перше. ")
    _wait_for_request_count(port, 1)
    accepted_before = stream.snapshot().accepted_characters

    with pytest.raises(SpeechError) as error:
        stream.feed("bad\ud800text")

    snapshot = stream.snapshot()
    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert snapshot.state is SpeechStreamState.RUNNING
    assert snapshot.accepted_characters == accepted_before

    stream.feed("Друге. ")
    _wait_for_request_count(port, 2)
    stream.finish()
    assert stream.wait(1)

    assert stream.snapshot().state is SpeechStreamState.COMPLETED
    assert [request.text for request in port.requests] == ["Перше.", "Друге."]


def test_pending_buffer_is_bounded_while_speech_is_blocked() -> None:
    port = BlockingSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Початок. ")
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

    assert stream.snapshot().state is SpeechStreamState.FAILED
    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert port.requests == []


def test_finish_without_any_fragment_fails_with_consistent_terminal_state() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.finish()
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.FAILED
    assert snapshot.accepted_characters == 0
    assert snapshot.spoken_characters == 0
    assert snapshot.chunk_count == 0
    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert port.requests == []


def test_spoken_chunk_then_trailing_whitespace_still_completes() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Готово. ")
    _wait_for_request_count(port, 1)
    stream.feed("   ")
    stream.finish()
    assert stream.wait(1)

    assert stream.snapshot().state is SpeechStreamState.COMPLETED
    assert stream.result().chunk_count == 1
    assert [request.text for request in port.requests] == ["Готово."]


def test_result_before_completion_is_retryable_busy() -> None:
    port = FakeSpeechPort()
    stream = IncrementalSpeechStream(port)

    with pytest.raises(SpeechError) as error:
        stream.result()

    assert error.value.code is SpeechErrorCode.ENGINE_BUSY
    assert error.value.retryable is True
    stream.cancel()
    assert stream.wait(1)


def test_result_does_not_expose_mutable_stored_failure() -> None:
    class FailingPort(FakeSpeechPort):
        def speak(
            self,
            request: SpeechRequest,
            *,
            timeout_seconds: float = 120.0,
            cancel_event: threading.Event | None = None,
        ) -> SpeechReceipt:
            del request, timeout_seconds, cancel_event
            raise SpeechError(
                SpeechErrorCode.ENGINE_UNAVAILABLE,
                "CANARY_ENGINE_DETAIL",
                retryable=True,
            )

    stream = IncrementalSpeechStream(FailingPort())
    stream.feed("Тест.")
    stream.finish()
    assert stream.wait(1)

    with pytest.raises(SpeechError) as first:
        stream.result()
    first.value.code = SpeechErrorCode.PROCESS_FAILED
    first.value.retryable = False

    with pytest.raises(SpeechError) as second:
        stream.result()

    assert second.value is not first.value
    assert second.value.code is SpeechErrorCode.ENGINE_UNAVAILABLE
    assert second.value.retryable is True
    assert "CANARY" not in str(second.value)


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


def test_fatal_port_failure_terminalizes_worker_state() -> None:
    class FatalPort(FakeSpeechPort):
        def speak(
            self,
            request: SpeechRequest,
            *,
            timeout_seconds: float = 120.0,
            cancel_event: threading.Event | None = None,
        ) -> SpeechReceipt:
            del request, timeout_seconds, cancel_event
            raise KeyboardInterrupt("CANARY_FATAL_PORT_DETAIL")

    stream = IncrementalSpeechStream(FatalPort())
    stream.feed("Приватний текст.")
    stream.finish()
    assert stream.wait(1)

    snapshot = stream.snapshot()
    assert snapshot.state is SpeechStreamState.FAILED
    assert snapshot.cancellation_requested is False

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
        lambda receipt: object.__setattr__(receipt, "engine_id", "bad\u202eengine"),
        lambda receipt: object.__setattr__(receipt, "voice_id", "bad\ud800voice"),
        lambda receipt: object.__setattr__(receipt, "engine_id", " test-engine"),
        lambda receipt: object.__setattr__(receipt, "voice_id", "voice-id "),
        lambda receipt: object.__setattr__(receipt, "rate", True),
        lambda receipt: object.__setattr__(receipt, "engine_id", "e" * 201),
        lambda receipt: object.__setattr__(receipt, "voice_id", "v" * 201),
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
    "voice_id",
    [
        " leading-space",
        "trailing-space ",
        "bidi-" + chr(0x202E) + "voice",
        "surrogate-" + chr(0xD800),
    ],
)
def test_constructor_rejects_invalid_voice_identity_before_audio_effect(
    voice_id: str,
) -> None:
    port = FakeSpeechPort()

    with pytest.raises(SpeechError) as error:
        IncrementalSpeechStream(port, voice_id=voice_id)

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert port.requests == []


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


def test_feed_after_cancel_is_rejected_without_accepting_more_text() -> None:
    port = CancelAwareSpeechPort()
    stream = IncrementalSpeechStream(port)

    stream.feed("Початок. ")
    assert port.started.wait(timeout=1)
    accepted_before_cancel = stream.snapshot().accepted_characters

    stream.cancel()
    with pytest.raises(SpeechError) as error:
        stream.feed(" Запізнілий текст.")

    assert error.value.code is SpeechErrorCode.INVALID_REQUEST
    assert stream.snapshot().accepted_characters == accepted_before_cancel
    assert stream.wait(1)
    assert stream.snapshot().state is SpeechStreamState.CANCELLED


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


def test_streaming_surface_is_exported_from_canonical_speech_package() -> None:
    from nika_core import speech

    assert speech.IncrementalSpeechStream is IncrementalSpeechStream
    assert speech.SpeechStreamState is SpeechStreamState
    assert speech.MAX_STREAM_PENDING_CHARS == MAX_STREAM_PENDING_CHARS
    assert speech.MAX_STREAM_TOTAL_CHARS == MAX_STREAM_TOTAL_CHARS
