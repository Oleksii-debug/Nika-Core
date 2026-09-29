from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nika_core.microphone_capture import MicrophoneCaptureEvidence, MicrophoneCaptureStatus
from nika_core.speech_to_text import SpeechToTextEvidence, SpeechToTextStatus
from nika_core.ui.bridge_models import UIResult
from nika_core.wake_activation import WakeActivationEvidence, WakeActivationOutcome
from nika_core.voice_turn import (
    OneShotVoiceTurnService,
    VoiceTurnEvidence,
    VoiceTurnRequest,
    VoiceTurnResult,
    VoiceTurnStatus,
)

VoiceRequestFactory = Callable[[str], VoiceTurnRequest]
VoiceSubmitter = Callable[
    [Coroutine[Any, Any, VoiceTurnResult]],
    Future[VoiceTurnResult],
]


class DesktopVoiceStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DesktopVoiceSnapshot:
    status: DesktopVoiceStatus
    request_id: str | None
    message: str
    activated: bool | None = None
    transcript: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.desktop-voice-state:v1",
            "status": self.status.value,
            "request_id": self.request_id,
            "message": self.message,
            "activated": self.activated,
            "transcript": self.transcript,
            "active": self.status in {
                DesktopVoiceStatus.RUNNING,
                DesktopVoiceStatus.CANCELLING,
            },
        }


class DesktopVoiceTurnController:
    """Synchronous desktop facade over the canonical async one-shot voice service.

    The controller owns no microphone, STT, wake, model, device or persistence
    authority. A trusted request factory supplies one exact VoiceTurnRequest for
    each generated request identity, and a desktop-owned async submitter runs the
    canonical OneShotVoiceTurnService.

    State is process-memory only. Raw PCM and component evidence are never
    projected through this desktop boundary.
    """

    def __init__(
        self,
        *,
        service: OneShotVoiceTurnService,
        request_factory: VoiceRequestFactory,
        submit: VoiceSubmitter,
    ) -> None:
        if type(service) is not OneShotVoiceTurnService:
            raise TypeError("service must be an exact OneShotVoiceTurnService")
        if not callable(request_factory):
            raise TypeError("request_factory must be callable")
        if not callable(submit):
            raise TypeError("submit must be callable")
        self._service = service
        self._request_factory = request_factory
        self._submit = submit
        self._lock = threading.Lock()
        self._active: Future[VoiceTurnResult] | None = None
        self._active_started: threading.Event | None = None
        self._active_settled: threading.Event | None = None
        self._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.IDLE,
            request_id=None,
            message="Голосовий ввід готовий.",
        )

    def start(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        with self._lock:
            self._reap_cancelled_settlement_locked()
            if self._active is not None:
                raise ValueError(
                    "Голосовий ввід уже виконується або завершує скасування."
                )
            request_id = f"desktop-voice-{uuid.uuid4().hex}"
            started = threading.Event()
            settled = threading.Event()
            self._snapshot = DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.RUNNING,
                request_id=request_id,
                message="Голосовий ввід розпочато. Говоріть після активації мікрофона.",
            )
            coroutine = self._execute(request_id, started, settled)
            try:
                future = self._submit(coroutine)
            except Exception as exc:
                coroutine.close()
                self._snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Не вдалося запустити голосовий ввід.",
                )
                raise ValueError("Не вдалося запустити голосовий ввід.") from exc
            if type(future) is not Future:
                coroutine.close()
                self._snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Desktop runtime повернув некоректний voice future.",
                )
                raise TypeError("submit must return concurrent.futures.Future")
            self._active = future
            self._active_started = started
            self._active_settled = settled

        future.add_done_callback(
            lambda done, identity=request_id: self._finish(identity, done)
        )
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Голосовий ввід прийнято до виконання.",
        )

    def cancel(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        with self._lock:
            self._reap_cancelled_settlement_locked()
            active = self._active
            settled = self._active_settled
            if active is None:
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Активного голосового вводу немає.",
                )
            if active.cancelled() and settled is not None and not settled.is_set():
                return UIResult(
                    request_id="desktop-handler",
                    status="accepted",
                    message="Скасування голосового вводу ще завершується.",
                )
            if active.done():
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Голосовий ввід уже завершився.",
                )

        accepted = active.cancel()
        if not accepted:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Голосовий ввід уже завершився.",
            )
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Запит на скасування голосового вводу прийнято.",
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._reap_cancelled_settlement_locked()
            return self._snapshot.as_dict()

    async def _execute(
        self,
        request_id: str,
        started: threading.Event,
        settled: threading.Event,
    ) -> VoiceTurnResult:
        started.set()
        try:
            request = await asyncio.to_thread(self._request_factory, request_id)
            if type(request) is not VoiceTurnRequest:
                raise TypeError("request_factory must return exact VoiceTurnRequest")
            if request.request_id != request_id:
                raise ValueError("voice request factory changed the generated request identity")
            return await self._service.run(request)
        finally:
            settled.set()

    def _finish(
        self,
        request_id: str,
        future: Future[VoiceTurnResult],
    ) -> None:
        if future.cancelled():
            with self._lock:
                if self._active is not future:
                    return
                started = self._active_started
                settled = self._active_settled
                if (
                    started is not None
                    and started.is_set()
                    and settled is not None
                    and not settled.is_set()
                ):
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.CANCELLING,
                        request_id=request_id,
                        message="Скасування голосового вводу завершується.",
                    )
                    return
                self._snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.CANCELLED,
                    request_id=request_id,
                    message="Голосовий ввід скасовано.",
                )
                self._clear_active_locked()
            return
        else:
            try:
                result = future.result()
            except Exception:
                snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий ввід завершився помилкою.",
                )
            else:
                snapshot = self._result_snapshot(request_id, result)

        with self._lock:
            if self._active is future:
                self._snapshot = snapshot
                self._clear_active_locked()

    def _reap_cancelled_settlement_locked(self) -> None:
        active = self._active
        settled = self._active_settled
        if (
            active is None
            or not active.cancelled()
            or settled is None
            or not settled.is_set()
        ):
            return
        self._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.CANCELLED,
            request_id=self._snapshot.request_id,
            message="Голосовий ввід скасовано.",
        )
        self._clear_active_locked()

    def _clear_active_locked(self) -> None:
        self._active = None
        self._active_started = None
        self._active_settled = None

    @staticmethod
    def _result_snapshot(
        request_id: str,
        result: object,
    ) -> DesktopVoiceSnapshot:
        if type(result) is not VoiceTurnResult:
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув некоректний результат.",
            )
        evidence = result.evidence
        if (
            type(evidence) is not VoiceTurnEvidence
            or type(evidence.request_id) is not str
            or type(evidence.status) is not VoiceTurnStatus
            or type(evidence.activated) is not bool
            or type(evidence.capture) is not MicrophoneCaptureEvidence
        ):
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув некоректні voice evidence.",
            )
        if (
            evidence.request_id != request_id
            or evidence.capture.request_id != request_id
        ):
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув неузгоджену ідентичність.",
            )
        if evidence.status is VoiceTurnStatus.COMPLETED:
            transcription = evidence.transcription
            wake = evidence.wake
            if (
                evidence.capture.status is not MicrophoneCaptureStatus.SUCCEEDED
                or type(transcription) is not SpeechToTextEvidence
                or transcription.status is not SpeechToTextStatus.SUCCEEDED
                or type(wake) is not WakeActivationEvidence
                or transcription.request_id != request_id
                or wake.request_id != request_id
                or type(result.transcript) is not str
                or not result.transcript
            ):
                return DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий сервіс повернув неузгоджений успішний результат.",
                )
            transcript_sha256 = hashlib.sha256(
                result.transcript.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            if (
                evidence.capture.audio_sha256 is None
                or transcription.audio_sha256 != evidence.capture.audio_sha256
                or transcription.transcript_sha256 != transcript_sha256
                or wake.transcript_sha256 != transcript_sha256
                or evidence.activated
                is not (wake.outcome is WakeActivationOutcome.DETECTED)
            ):
                return DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий сервіс повернув неузгоджені доказові дані.",
                )
            message = (
                "Мовлення розпізнано; ключову фразу Nika виявлено."
                if evidence.activated
                else "Мовлення розпізнано; ключову фразу Nika не виявлено."
            )
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.COMPLETED,
                request_id=request_id,
                message=message,
                activated=evidence.activated,
                transcript=result.transcript,
            )

        if result.transcript is not None or evidence.activated:
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув дані, несумісні зі станом помилки.",
            )
        if evidence.transcription is not None and (
            type(evidence.transcription) is not SpeechToTextEvidence
            or evidence.transcription.request_id != request_id
        ):
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув некоректні STT evidence.",
            )
        if evidence.wake is not None and (
            type(evidence.wake) is not WakeActivationEvidence
            or evidence.wake.request_id != request_id
        ):
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув некоректні wake evidence.",
            )

        messages = {
            VoiceTurnStatus.CAPTURE_FAILED: "Не вдалося отримати звук із мікрофона.",
            VoiceTurnStatus.TRANSCRIPTION_FAILED: "Не вдалося розпізнати мовлення.",
            VoiceTurnStatus.INVALID_COMPOSITION: (
                "Голосовий ланцюжок повернув неузгоджені дані."
            ),
        }
        return DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.FAILED,
            request_id=request_id,
            message=messages.get(
                evidence.status,
                "Голосовий ввід завершився невідомим станом.",
            ),
        )

    @staticmethod
    def _require_empty_payload(payload: Mapping[str, Any]) -> None:
        if type(payload) is not dict:
            raise TypeError("voice desktop action payload must be an exact dict")
        if payload:
            raise ValueError("voice desktop action does not accept payload authority")
