from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Protocol

MAX_SPEECH_TEXT_CHARS = 20_000
MAX_VOICE_ID_CHARS = 200


class SpeechErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_BUSY = "engine_busy"
    VOICE_UNAVAILABLE = "voice_unavailable"
    PROCESS_FAILED = "process_failed"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_CANCELLED = "process_cancelled"
    INVALID_ENGINE_RESPONSE = "invalid_engine_response"


class SpeechError(RuntimeError):
    def __init__(
        self,
        code: SpeechErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    text: str
    voice_id: str | None = None
    rate: int = 0
    volume: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech text must be a string")
        if not self.text.strip():
            raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech text must not be empty")
        if len(self.text) > MAX_SPEECH_TEXT_CHARS:
            raise SpeechError(
                SpeechErrorCode.INVALID_REQUEST,
                f"speech text exceeds {MAX_SPEECH_TEXT_CHARS} characters",
            )
        if "\x00" in self.text:
            raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech text must not contain NUL")
        if type(self.rate) is not int or not -10 <= self.rate <= 10:
            raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech rate must be -10..10")
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise SpeechError(SpeechErrorCode.INVALID_REQUEST, "speech volume must be 0..100")
        if self.voice_id is not None:
            if not isinstance(self.voice_id, str) or not self.voice_id.strip():
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    "voice_id must be a non-empty string when supplied",
                )
            if len(self.voice_id) > MAX_VOICE_ID_CHARS:
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    f"voice_id exceeds {MAX_VOICE_ID_CHARS} characters",
                )
            if any(ord(char) < 32 for char in self.voice_id):
                raise SpeechError(
                    SpeechErrorCode.INVALID_REQUEST,
                    "voice_id must not contain control characters",
                )


@dataclass(frozen=True, slots=True)
class SpeechVoice:
    voice_id: str
    culture: str | None
    gender: str | None
    age: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class SpeechReceipt:
    engine_id: str
    voice_id: str
    character_count: int
    rate: int
    volume: int


class SpeechOutputPort(Protocol):
    def list_voices(self, *, timeout_seconds: float = 10.0) -> tuple[SpeechVoice, ...]: ...

    def speak(
        self,
        request: SpeechRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: Event | None = None,
    ) -> SpeechReceipt: ...
