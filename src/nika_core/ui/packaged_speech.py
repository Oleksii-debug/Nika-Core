from __future__ import annotations

import sys
from threading import Lock
from typing import Any

from nika_core.speech import (
    IncrementalSpeechStream,
    SpeechError,
    SpeechRequest,
    SpeechStreamState,
    WindowsSystemSpeechAdapter,
)
from nika_core.ui.bridge_models import UIResult


class PackagedSpeechFeature:
    """Explicit packaged speech-output facade over canonical streaming TTS."""

    def __init__(
        self,
        *,
        output: object | None,
        unavailable_message: str | None = None,
    ) -> None:
        if output is None:
            if type(unavailable_message) is not str or not unavailable_message:
                raise ValueError("unavailable packaged speech requires a bounded message")
        elif unavailable_message is not None:
            raise ValueError("available packaged speech must not carry unavailable text")
        self._output = output
        self._unavailable_message = unavailable_message
        self._lock = Lock()
        self._stream: IncrementalSpeechStream | None = None
        self._generation = 0
        self._closed = False

    @property
    def available(self) -> bool:
        return self._output is not None

    def speak(self, payload: dict[str, Any]) -> UIResult:
        if type(payload) is not dict:
            raise TypeError("packaged speech action payload must be an exact dict")
        if set(payload) != {"text"}:
            raise ValueError("packaged speech start requires only text")
        text = payload.get("text")
        if type(text) is not str:
            raise TypeError("packaged speech text must be an exact string")

        if self._output is None:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=self._unavailable_message or "Озвучення недоступне.",
                focus_id="speech-heading",
            )

        try:
            validated = SpeechRequest(text)
        except SpeechError:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message="Текст для озвучення порожній, пошкоджений або завеликий.",
                focus_id="speech-text",
            )

        with self._lock:
            if self._closed:
                return UIResult(
                    request_id="desktop-handler",
                    status="rejected",
                    message="Озвучення вже завершило роботу разом із застосунком.",
                    focus_id="speech-heading",
                )
            current = self._stream
            if current is not None and current.snapshot().state in {
                SpeechStreamState.RUNNING,
                SpeechStreamState.DRAINING,
            }:
                return UIResult(
                    request_id="desktop-handler",
                    status="rejected",
                    message="Попереднє озвучення ще триває. Спочатку скасуйте його.",
                    focus_id="speech-cancel",
                )
            try:
                stream = IncrementalSpeechStream(self._output)
                stream.feed(validated.text)
                stream.finish()
            except SpeechError:
                return UIResult(
                    request_id="desktop-handler",
                    status="failed",
                    message="Не вдалося запустити локальне озвучення Windows.",
                    focus_id="speech-heading",
                )
            self._stream = stream
            self._generation += 1

        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Озвучення розпочато.",
            focus_id="speech-cancel",
        )

    def cancel(self, payload: dict[str, Any]) -> UIResult:
        if type(payload) is not dict:
            raise TypeError("packaged speech action payload must be an exact dict")
        if payload:
            raise ValueError("packaged speech cancel does not accept payload authority")
        with self._lock:
            stream = self._stream
            snapshot = stream.snapshot() if stream is not None else None
            if snapshot is None or snapshot.state in {
                SpeechStreamState.COMPLETED,
                SpeechStreamState.CANCELLED,
                SpeechStreamState.FAILED,
            }:
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Активного озвучення немає.",
                    focus_id="speech-start",
                )
            if snapshot.cancellation_requested:
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Скасування озвучення вже запитано.",
                    focus_id="speech-heading",
                )
            stream.cancel()
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Скасування озвучення запитано.",
            focus_id="speech-heading",
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            available = self._output is not None
            generation = self._generation
            stream = self._stream
            unavailable_message = self._unavailable_message
        if not available:
            return {
                "schema": "nika.packaged-speech-state:v1",
                "available": False,
                "status": "unavailable",
                "generation": generation,
                "active": False,
                "message": unavailable_message,
                "accepted_characters": 0,
                "spoken_characters": 0,
                "chunk_count": 0,
                "pending_characters": 0,
            }
        if stream is None:
            return {
                "schema": "nika.packaged-speech-state:v1",
                "available": True,
                "status": "idle",
                "generation": generation,
                "active": False,
                "message": "Локальне озвучення Windows готове.",
                "accepted_characters": 0,
                "spoken_characters": 0,
                "chunk_count": 0,
                "pending_characters": 0,
            }
        snapshot = stream.snapshot()

        messages = {
            SpeechStreamState.RUNNING: "Озвучення виконується.",
            SpeechStreamState.DRAINING: "Озвучення завершує чергу тексту.",
            SpeechStreamState.COMPLETED: "Озвучення завершено.",
            SpeechStreamState.CANCELLED: "Озвучення скасовано.",
            SpeechStreamState.FAILED: "Локальне озвучення завершилося з помилкою.",
        }
        public_status = snapshot.state.value
        message = messages[snapshot.state]
        if snapshot.cancellation_requested and snapshot.state in {
            SpeechStreamState.RUNNING,
            SpeechStreamState.DRAINING,
        }:
            public_status = "cancelling"
            message = "Скасування озвучення виконується."
        return {
            "schema": "nika.packaged-speech-state:v1",
            "available": True,
            "status": public_status,
            "generation": generation,
            "active": snapshot.state in {
                SpeechStreamState.RUNNING,
                SpeechStreamState.DRAINING,
            },
            "message": message,
            "accepted_characters": snapshot.accepted_characters,
            "spoken_characters": snapshot.spoken_characters,
            "chunk_count": snapshot.chunk_count,
            "pending_characters": snapshot.pending_characters,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
            if stream is None:
                return
            if stream.snapshot().state in {
                SpeechStreamState.RUNNING,
                SpeechStreamState.DRAINING,
            }:
                stream.cancel()
        if not stream.wait(5.0):
            raise RuntimeError("packaged speech worker did not settle during shutdown")


def build_packaged_speech() -> PackagedSpeechFeature:
    """Build explicit local Windows speech output with no network/model fallback."""

    if sys.platform != "win32":
        return PackagedSpeechFeature(
            output=None,
            unavailable_message="Озвучення доступне лише у застосунку Windows.",
        )
    try:
        output = WindowsSystemSpeechAdapter()
    except SpeechError:
        return PackagedSpeechFeature(
            output=None,
            unavailable_message=(
                "Локальне озвучення Windows недоступне. "
                "Перевірте системний компонент Windows System.Speech."
            ),
        )
    return PackagedSpeechFeature(output=output)
