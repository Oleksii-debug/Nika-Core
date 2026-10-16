from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, Event, Thread
from typing import Protocol

from nika_core.speech.contracts import (
    MAX_SPEECH_TEXT_CHARS,
    MAX_VOICE_ID_CHARS,
    SpeechError,
    SpeechErrorCode,
    SpeechOutputPort,
    SpeechReceipt,
    SpeechRequest,
)

DEFAULT_STREAM_CHUNK_CHARS = 600
MAX_STREAM_PENDING_CHARS = 40_000
MAX_STREAM_TOTAL_CHARS = 200_000
MAX_STREAM_ENGINE_ID_CHARS = 200


class SpeechStreamState(StrEnum):
    RUNNING = "running"
    DRAINING = "draining"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SpeechStreamSnapshot:
    state: SpeechStreamState
    accepted_characters: int
    spoken_characters: int
    chunk_count: int
    pending_characters: int
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class StreamingSpeechReceipt:
    engine_id: str
    voice_id: str
    chunk_count: int
    character_count: int
    rate: int
    volume: int


class _SpeechPort(Protocol):
    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt: ...


class IncrementalSpeechStream:
    """Bounded producer/consumer composition over the canonical speech output port."""

    def __init__(
        self,
        output: SpeechOutputPort | _SpeechPort,
        *,
        voice_id: str | None = None,
        rate: int = 0,
        volume: int = 100,
        chunk_chars: int = DEFAULT_STREAM_CHUNK_CHARS,
        timeout_seconds: float = 120.0,
    ) -> None:
        settings = SpeechRequest("x", voice_id=voice_id, rate=rate, volume=volume)
        if settings.voice_id is not None and (
            settings.voice_id != settings.voice_id.strip()
            or any(
                unicodedata.category(char).startswith("C")
                for char in settings.voice_id
            )
        ):
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                "speech stream voice identity is invalid",
            )
        if type(chunk_chars) is not int or not 1 <= chunk_chars <= MAX_SPEECH_TEXT_CHARS:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                f"stream chunk size must be 1..{MAX_SPEECH_TEXT_CHARS}",
            )
        timeout = _validate_timeout(timeout_seconds)

        self._output = output
        self._voice_id = settings.voice_id
        self._rate = settings.rate
        self._volume = settings.volume
        self._chunk_chars = chunk_chars
        self._timeout_seconds = timeout

        self._condition = Condition()
        self._cancel_event = Event()
        self._cancellation_requested = False
        self._incoming = ""
        self._buffered_characters = 0
        self._finish_requested = False
        self._state = SpeechStreamState.RUNNING
        self._accepted_characters = 0
        self._spoken_characters = 0
        self._chunk_count = 0
        self._engine_id: str | None = None
        self._selected_voice_id: str | None = None
        self._failure: SpeechError | None = None

        self._worker = Thread(
            target=self._run,
            name="nika-incremental-speech",
            daemon=True,
        )
        try:
            self._worker.start()
        except RuntimeError:
            raise SpeechError(
                SpeechErrorCode.PROCESS_FAILED,
                "speech stream worker could not be started",
            ) from None

    def feed(self, fragment: str) -> None:
        if type(fragment) is not str:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                "speech stream fragment must be a string",
            )
        if "\x00" in fragment:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                "speech stream fragment must not contain NUL",
            )
        try:
            fragment.encode("utf-8")
        except UnicodeEncodeError:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                "speech stream fragment must be valid UTF-8 text",
            ) from None
        with self._condition:
            if (
                self._state is not SpeechStreamState.RUNNING
                or self._cancellation_requested
            ):
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    "speech stream no longer accepts input",
                )
            if not fragment:
                return
            next_total = self._accepted_characters + len(fragment)
            if next_total > MAX_STREAM_TOTAL_CHARS:
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    f"speech stream exceeds {MAX_STREAM_TOTAL_CHARS} characters",
                )
            if self._buffered_characters + len(fragment) > MAX_STREAM_PENDING_CHARS:
                raise SpeechError(
                    SpeechErrorCode.ENGINE_BUSY,
                    "speech stream producer exceeded the pending buffer",
                    retryable=True,
                )
            self._incoming += fragment
            self._buffered_characters += len(fragment)
            self._accepted_characters = next_total
            self._condition.notify_all()

    def finish(self) -> None:
        with self._condition:
            if self._state in (
                SpeechStreamState.COMPLETED,
                SpeechStreamState.CANCELLED,
                SpeechStreamState.FAILED,
            ):
                return
            self._finish_requested = True
            self._state = SpeechStreamState.DRAINING
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            if self._state in (
                SpeechStreamState.COMPLETED,
                SpeechStreamState.CANCELLED,
                SpeechStreamState.FAILED,
            ):
                return
            self._cancellation_requested = True
            self._cancel_event.set()
            self._finish_requested = True
            self._condition.notify_all()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        timeout = _validate_wait_timeout(timeout_seconds)
        self._worker.join(timeout=timeout)
        return not self._worker.is_alive()

    def snapshot(self) -> SpeechStreamSnapshot:
        with self._condition:
            return SpeechStreamSnapshot(
                state=self._state,
                accepted_characters=self._accepted_characters,
                spoken_characters=self._spoken_characters,
                chunk_count=self._chunk_count,
                pending_characters=self._buffered_characters,
                cancellation_requested=self._cancellation_requested,
            )

    def result(self) -> StreamingSpeechReceipt:
        if self._worker.is_alive():
            raise SpeechError(
                SpeechErrorCode.ENGINE_BUSY,
                "speech stream is still active",
                retryable=True,
            )
        with self._condition:
            if self._failure is not None:
                raise SpeechError(
                    self._failure.code,
                    str(self._failure),
                    retryable=self._failure.retryable,
                )
            if self._state is SpeechStreamState.CANCELLED:
                raise SpeechError(
                    SpeechErrorCode.PROCESS_CANCELLED,
                    "speech stream was cancelled",
                )
            if self._state is not SpeechStreamState.COMPLETED:
                raise SpeechError(
                    SpeechErrorCode.PROCESS_FAILED,
                    "speech stream did not complete",
                )
            if self._engine_id is None or self._selected_voice_id is None:
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    "speech stream contained no speakable text",
                )
            return StreamingSpeechReceipt(
                engine_id=self._engine_id,
                voice_id=self._selected_voice_id,
                chunk_count=self._chunk_count,
                character_count=self._spoken_characters,
                rate=self._rate,
                volume=self._volume,
            )

    def _run(self) -> None:
        pending = ""
        selected_voice_id = self._voice_id
        while True:
            with self._condition:
                while (
                    not self._incoming
                    and not self._finish_requested
                    and not self._cancellation_requested
                    and not _has_ready_chunk(pending, chunk_chars=self._chunk_chars)
                ):
                    self._condition.wait()

                if self._cancellation_requested:
                    self._cancel_event.set()
                    self._state = SpeechStreamState.CANCELLED
                    self._condition.notify_all()
                    return

                if self._incoming:
                    pending += self._incoming
                    self._incoming = ""

                finish_requested = self._finish_requested

            chunk, pending, consumed_characters = _pop_ready_chunk(
                pending,
                chunk_chars=self._chunk_chars,
                flush=finish_requested,
            )
            if consumed_characters:
                with self._condition:
                    self._buffered_characters -= consumed_characters
            if chunk is None:
                if finish_requested:
                    if pending.strip():
                        self._fail(
                            SpeechError(
                                SpeechErrorCode.PROCESS_FAILED,
                                "speech stream could not flush pending text",
                            )
                        )
                    else:
                        with self._condition:
                            has_spoken_chunks = self._chunk_count > 0
                        if has_spoken_chunks:
                            self._complete()
                        else:
                            self._fail(
                                SpeechError(
                                    SpeechErrorCode.INVALID_REQUEST,
                                    "speech stream contained no speakable text",
                                )
                            )
                    return
                continue
            if not chunk:
                continue

            try:
                receipt = self._output.speak(
                    SpeechRequest(
                        chunk,
                        voice_id=selected_voice_id,
                        rate=self._rate,
                        volume=self._volume,
                    ),
                    timeout_seconds=self._timeout_seconds,
                    cancel_event=self._cancel_event,
                )
            except SpeechError as exc:
                with self._condition:
                    cancellation_requested = self._cancellation_requested
                    if cancellation_requested:
                        self._cancel_event.set()
                    else:
                        self._cancel_event.clear()
                if (
                    cancellation_requested
                    and type(exc) is SpeechError
                    and type(exc.code) is SpeechErrorCode
                    and exc.code is SpeechErrorCode.PROCESS_CANCELLED
                ):
                    self._cancel()
                    return
                if (
                    type(exc) is SpeechError
                    and type(exc.code) is SpeechErrorCode
                    and exc.code is SpeechErrorCode.PROCESS_CANCELLED
                ):
                    self._fail(
                        SpeechError(
                            SpeechErrorCode.PROCESS_FAILED,
                            "streaming speech output failed",
                        )
                    )
                    return
                self._fail(_sanitize_speech_error(exc))
                return
            except Exception:  # noqa: BLE001 - external port diagnostics must not escape
                self._fail(
                    SpeechError(
                        SpeechErrorCode.PROCESS_FAILED,
                        "streaming speech output failed",
                    )
                )
                return
            except BaseException:  # noqa: BLE001 - worker boundary must terminalize
                self._fail(
                    SpeechError(
                        SpeechErrorCode.PROCESS_FAILED,
                        "streaming speech output failed",
                    )
                )
                return

            try:
                clean = _validate_receipt(
                    receipt,
                    expected_characters=len(chunk),
                    expected_rate=self._rate,
                    expected_volume=self._volume,
                )
                if self._engine_id is not None and clean.engine_id != self._engine_id:
                    raise SpeechError(
                        SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                        "speech stream engine identity changed",
                    )
                if selected_voice_id is not None:
                    initial_voice = self._selected_voice_id is None
                    voice_changed = (
                        clean.voice_id.casefold() != selected_voice_id.casefold()
                        if initial_voice
                        else clean.voice_id != selected_voice_id
                    )
                    if voice_changed:
                        raise SpeechError(
                            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
                            "speech stream voice identity changed",
                        )
            except SpeechError as exc:
                self._fail(exc)
                return

            selected_voice_id = clean.voice_id
            with self._condition:
                if self._engine_id is None:
                    self._engine_id = clean.engine_id
                self._selected_voice_id = clean.voice_id
                self._spoken_characters += clean.character_count
                self._chunk_count += 1
                cancellation_requested = self._cancellation_requested
                if cancellation_requested:
                    self._cancel_event.set()
                else:
                    self._cancel_event.clear()

            if cancellation_requested:
                self._cancel()
                return

    def _cancel(self) -> None:
        with self._condition:
            self._state = SpeechStreamState.CANCELLED
            self._condition.notify_all()

    def _fail(self, error: SpeechError) -> None:
        with self._condition:
            self._failure = error
            self._state = SpeechStreamState.FAILED
            self._condition.notify_all()

    def _complete(self) -> None:
        with self._condition:
            self._state = SpeechStreamState.COMPLETED
            self._condition.notify_all()


def _has_ready_chunk(text: str, *, chunk_chars: int) -> bool:
    return (
        _first_sentence_boundary(text, limit=chunk_chars) is not None
        or len(text) >= chunk_chars
    )


def _pop_ready_chunk(
    text: str,
    *,
    chunk_chars: int,
    flush: bool,
) -> tuple[str | None, str, int]:
    if not text:
        return None, text, 0

    boundary = _first_sentence_boundary(text, limit=chunk_chars)
    if boundary is None and len(text) >= chunk_chars:
        boundary = _bounded_word_boundary(text, limit=chunk_chars)
    if boundary is None and flush:
        boundary = min(len(text), chunk_chars)
    if boundary is None:
        return None, text, 0

    raw = text[:boundary]
    remaining = text[boundary:]
    return raw.strip(), remaining, len(raw)


def _first_sentence_boundary(text: str, *, limit: int) -> int | None:
    closing = "\"'”’»)]}"
    scan_limit = min(len(text), limit)
    for index, char in enumerate(text[:scan_limit]):
        if char == "\n":
            return index + 1
        if char not in ".!?…":
            continue
        cursor = index + 1
        while cursor < scan_limit and text[cursor] in closing:
            cursor += 1
        if cursor >= scan_limit or not text[cursor].isspace():
            continue
        while cursor < scan_limit and text[cursor].isspace():
            cursor += 1
        return cursor
    return None


def _bounded_word_boundary(text: str, *, limit: int) -> int:
    candidate = text[:limit]
    for separator in ("\n", " ", "\t"):
        index = candidate.rfind(separator)
        if index > 0:
            return index + 1
    return limit


def _validate_receipt(
    receipt: object,
    *,
    expected_characters: int,
    expected_rate: int,
    expected_volume: int,
) -> SpeechReceipt:
    if type(receipt) is not SpeechReceipt:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid receipt",
        )

    engine_id = receipt.engine_id
    voice_id = receipt.voice_id
    character_count = receipt.character_count
    rate = receipt.rate
    volume = receipt.volume

    if (
        type(engine_id) is not str
        or not engine_id.strip()
        or engine_id != engine_id.strip()
        or len(engine_id) > MAX_STREAM_ENGINE_ID_CHARS
    ):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid engine identity",
        )
    if (
        type(voice_id) is not str
        or not voice_id.strip()
        or voice_id != voice_id.strip()
        or len(voice_id) > MAX_VOICE_ID_CHARS
    ):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid voice identity",
        )
    if any(unicodedata.category(char).startswith("C") for char in engine_id + voice_id):
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid route identity",
        )
    if type(character_count) is not int or character_count != expected_characters:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid character count",
        )
    if type(rate) is not int or rate != expected_rate:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid rate",
        )
    if type(volume) is not int or volume != expected_volume:
        raise SpeechError(
            SpeechErrorCode.INVALID_ENGINE_RESPONSE,
            "speech output returned an invalid volume",
        )
    return SpeechReceipt(
        engine_id=engine_id,
        voice_id=voice_id,
        character_count=character_count,
        rate=rate,
        volume=volume,
    )


def _sanitize_speech_error(error: SpeechError) -> SpeechError:
    if type(error) is not SpeechError:
        return SpeechError(
            SpeechErrorCode.PROCESS_FAILED,
            "streaming speech output failed",
        )
    code = error.code if type(error.code) is SpeechErrorCode else SpeechErrorCode.PROCESS_FAILED
    retryable = error.retryable if type(error.retryable) is bool else False
    return SpeechError(code, "streaming speech output failed", retryable=retryable)


def _validate_timeout(timeout_seconds: object) -> float:
    if type(timeout_seconds) not in (int, float):
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream timeout must be numeric",
        )
    try:
        timeout = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream timeout is outside the supported bound",
        ) from None
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream timeout must be finite, greater than 0, and at most 3600 seconds",
        )
    return timeout


def _validate_wait_timeout(timeout_seconds: object) -> float | None:
    if timeout_seconds is None:
        return None
    if type(timeout_seconds) not in (int, float):
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream wait timeout must be numeric",
        )
    try:
        timeout = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream wait timeout is outside the supported bound",
        ) from None
    if not math.isfinite(timeout) or timeout < 0 or timeout > 3600:
        raise SpeechError(
            SpeechErrorCode.INVALID_REQUEST,
            "speech stream wait timeout must be finite and 0..3600 seconds",
        )
    return timeout
