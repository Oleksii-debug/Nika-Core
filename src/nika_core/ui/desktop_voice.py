from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nika_core.microphone_capture import MicrophoneCaptureEvidence, MicrophoneCaptureStatus
from nika_core.model_gateway.contracts import PrivacyClass
from nika_core.speech_to_text import (
    SpeechAudioFormat,
    SpeechToTextEvidence,
    SpeechToTextStatus,
)
from nika_core.ui.bridge_models import UIResult
from nika_core.voice_turn import (
    OneShotVoiceTurnService,
    VoiceTurnEvidence,
    VoiceTurnRequest,
    VoiceTurnResult,
    VoiceTurnStatus,
)
from nika_core.wake_activation import (
    MAX_TRANSCRIPT_CHARS,
    WakeActivationEvidence,
    WakeActivationOutcome,
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
        self._active_cancel_intent = False
        self._submitting_request_id: str | None = None
        self._cancel_requested_while_submitting = False
        self._submission_settled: threading.Event | None = None
        self._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.IDLE,
            request_id=None,
            message="Голосовий ввід готовий.",
        )

    def start(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        with self._lock:
            self._reap_cancelled_settlement_locked()
            if self._active is not None or self._submitting_request_id is not None:
                raise ValueError(
                    "Голосовий ввід уже виконується або завершує скасування."
                )
            request_id = f"desktop-voice-{uuid.uuid4().hex}"
            started = threading.Event()
            settled = threading.Event()
            submission_settled = threading.Event()
            self._submitting_request_id = request_id
            self._cancel_requested_while_submitting = False
            self._submission_settled = submission_settled
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
            with self._lock:
                if self._submitting_request_id == request_id:
                    self._clear_submission_locked()
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.FAILED,
                        request_id=request_id,
                        message="Не вдалося запустити голосовий ввід.",
                    )
            submission_settled.set()
            raise RuntimeError("Не вдалося запустити голосовий ввід.") from exc
        except BaseException:
            coroutine.close()
            with self._lock:
                if self._submitting_request_id == request_id:
                    self._clear_submission_locked()
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.FAILED,
                        request_id=request_id,
                        message="Не вдалося запустити голосовий ввід.",
                    )
            submission_settled.set()
            raise
        if type(future) is not Future:
            coroutine.close()
            with self._lock:
                if self._submitting_request_id == request_id:
                    self._clear_submission_locked()
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.FAILED,
                        request_id=request_id,
                        message="Desktop runtime повернув некоректний voice future.",
                    )
            submission_settled.set()
            raise TypeError("submit must return concurrent.futures.Future")

        with self._lock:
            if self._submitting_request_id != request_id:
                coroutine.close()
                future.cancel()
                raise RuntimeError("voice submission reservation was lost")
            cancel_requested = self._cancel_requested_while_submitting
            self._clear_submission_locked()
            self._active = future
            self._active_started = started
            self._active_settled = settled
            self._active_cancel_intent = cancel_requested

        submission_settled.set()
        future.add_done_callback(
            lambda done, identity=request_id, cancelled=cancel_requested: self._finish(
                identity,
                done,
                cancellation_requested=cancelled,
            )
        )
        if cancel_requested and not future.done():
            future.cancel()
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="Голосовий ввід прийнято до виконання.",
        )

    def cancel(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        with self._lock:
            self._reap_cancelled_settlement_locked()
            if self._submitting_request_id is not None:
                self._cancel_requested_while_submitting = True
                self._snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.CANCELLING,
                    request_id=self._submitting_request_id,
                    message="Скасування голосового вводу завершується.",
                )
                return UIResult(
                    request_id="desktop-handler",
                    status="accepted",
                    message="Запит на скасування голосового вводу прийнято.",
                )
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

        with self._lock:
            if self._active is active:
                self._active_cancel_intent = True

        accepted = active.cancel()
        if not accepted:
            if active.done():
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message="Голосовий ввід уже завершився.",
                )
            with self._lock:
                if self._active is active:
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.CANCELLING,
                        request_id=self._snapshot.request_id,
                        message=(
                            "Скасування не підтверджено runtime; "
                            "результат голосового вводу буде приховано."
                        ),
                    )
            return UIResult(
                request_id="desktop-handler",
                status="accepted",
                message="Запит на скасування збережено до завершення голосового вводу.",
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

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        if type(timeout_seconds) not in (int, float):
            raise TypeError("timeout_seconds must be an exact number")
        try:
            timeout = float(timeout_seconds)
        except OverflowError:
            timeout = float("inf")
        if not math.isfinite(timeout) or timeout <= 0.0 or timeout > 30.0:
            raise ValueError("timeout_seconds must be finite and in the range (0, 30]")

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._reap_cancelled_settlement_locked()
                if self._submitting_request_id is not None:
                    self._cancel_requested_while_submitting = True
                    submission_settled = self._submission_settled
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.CANCELLING,
                        request_id=self._submitting_request_id,
                        message="Скасування голосового вводу завершується.",
                    )
                    active = None
                    started = None
                    settled = None
                else:
                    submission_settled = None
                    active = self._active
                    started = self._active_started
                    settled = self._active_settled
            if submission_settled is None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.0 or not submission_settled.wait(timeout=remaining):
                raise RuntimeError(
                    "voice submission did not settle before the desktop shutdown deadline"
                )

        if active is None:
            return
        if not active.done():
            with self._lock:
                if self._active is active:
                    self._active_cancel_intent = True
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.CANCELLING,
                        request_id=self._snapshot.request_id,
                        message="Скасування голосового вводу завершується.",
                    )
            active.cancel()

        remaining = max(0.0, deadline - time.monotonic())
        if (
            started is not None
            and started.is_set()
            and settled is not None
            and not settled.wait(timeout=remaining)
        ):
            raise RuntimeError(
                "voice coroutine did not settle before the desktop shutdown deadline"
            )

        with self._lock:
            self._reap_cancelled_settlement_locked()
            if self._active is active:
                if active.done() and not active.cancelled():
                    raise RuntimeError(
                        "voice completion callback did not settle controller state"
                    )
                raise RuntimeError(
                    "voice turn remained active after the desktop shutdown deadline"
                )

    async def _execute(
        self,
        request_id: str,
        started: threading.Event,
        settled: threading.Event,
    ) -> VoiceTurnResult:
        started.set()
        loop = asyncio.get_running_loop()
        factory_future = loop.run_in_executor(
            None,
            self._request_factory,
            request_id,
        )
        try:
            request = await asyncio.shield(factory_future)
            if type(request) is not VoiceTurnRequest:
                raise TypeError("request_factory must return exact VoiceTurnRequest")
            if (
                type(request.request_id) is not str
                or request.request_id != request_id
            ):
                raise ValueError("voice request factory changed the generated request identity")
            return await self._service.run(request)
        finally:
            if factory_future.done():
                settled.set()
            else:
                factory_future.add_done_callback(
                    lambda done: _settle_late_factory_future(done, settled)
                )

    def _finish(
        self,
        request_id: str,
        future: Future[VoiceTurnResult],
        *,
        cancellation_requested: bool = False,
    ) -> None:
        if type(cancellation_requested) is not bool:
            raise TypeError("cancellation_requested must be an exact bool")
        with self._lock:
            cancellation_requested = cancellation_requested or (
                self._active is future and self._active_cancel_intent
            )
        if cancellation_requested and not future.cancelled():
            future.exception()
            with self._lock:
                if self._active is future:
                    self._snapshot = DesktopVoiceSnapshot(
                        status=DesktopVoiceStatus.CANCELLED,
                        request_id=request_id,
                        message="Голосовий ввід скасовано.",
                    )
                    self._clear_active_locked()
            return
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
            failure = future.exception()
            if failure is not None:
                snapshot = DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий ввід завершився помилкою.",
                )
            else:
                snapshot = self._result_snapshot(request_id, future.result())

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
        self._active_cancel_intent = False

    def _clear_submission_locked(self) -> None:
        self._submitting_request_id = None
        self._cancel_requested_while_submitting = False
        self._submission_settled = None

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
            or type(evidence.capture.request_id) is not str
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
                or type(wake.outcome) is not WakeActivationOutcome
                or type(transcription.request_id) is not str
                or type(wake.request_id) is not str
                or transcription.request_id != request_id
                or wake.request_id != request_id
                or not _is_public_transcript(result.transcript)
                or type(transcription.transcript_chars) is not int
                or transcription.transcript_chars != len(result.transcript)
                or type(evidence.capture.audio_byte_count) is not int
                or type(evidence.capture.sample_rate_hz) is not int
                or type(transcription.audio_bytes) is not int
                or type(transcription.sample_rate_hz) is not int
                or type(transcription.channels) is not int
                or transcription.audio_bytes != evidence.capture.audio_byte_count
                or transcription.sample_rate_hz != evidence.capture.sample_rate_hz
                or transcription.channels != 1
                or transcription.audio_format is not SpeechAudioFormat.PCM_S16LE
                or transcription.privacy is not PrivacyClass.SENSITIVE
                or not _is_valid_wake_evidence(wake)
            ):
                return DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий сервіс повернув неузгоджений успішний результат.",
                )
            if (
                not _is_sha256(evidence.capture.audio_sha256)
                or not _is_sha256(transcription.audio_sha256)
                or not _is_sha256(transcription.transcript_sha256)
                or not _is_sha256(wake.transcript_sha256)
            ):
                return DesktopVoiceSnapshot(
                    status=DesktopVoiceStatus.FAILED,
                    request_id=request_id,
                    message="Голосовий сервіс повернув некоректні доказові поля.",
                )
            transcript_sha256 = hashlib.sha256(
                result.transcript.encode("utf-8")
            ).hexdigest()
            if (
                transcription.audio_sha256 != evidence.capture.audio_sha256
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
        if not _is_valid_failure_evidence(evidence, request_id):
            return DesktopVoiceSnapshot(
                status=DesktopVoiceStatus.FAILED,
                request_id=request_id,
                message="Голосовий сервіс повернув неузгоджені failure evidence.",
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


def _is_valid_failure_evidence(
    evidence: VoiceTurnEvidence,
    request_id: str,
) -> bool:
    capture_status = evidence.capture.status
    if type(capture_status) is not MicrophoneCaptureStatus:
        return False

    transcription = evidence.transcription
    if transcription is not None and (
        type(transcription) is not SpeechToTextEvidence
        or type(transcription.request_id) is not str
        or type(transcription.status) is not SpeechToTextStatus
        or transcription.request_id != request_id
    ):
        return False

    wake = evidence.wake
    if wake is not None and (
        type(wake) is not WakeActivationEvidence
        or type(wake.request_id) is not str
        or type(wake.outcome) is not WakeActivationOutcome
        or wake.request_id != request_id
    ):
        return False

    if evidence.status is VoiceTurnStatus.CAPTURE_FAILED:
        return (
            capture_status is not MicrophoneCaptureStatus.SUCCEEDED
            and transcription is None
            and wake is None
        )
    if evidence.status is VoiceTurnStatus.TRANSCRIPTION_FAILED:
        return (
            capture_status is MicrophoneCaptureStatus.SUCCEEDED
            and transcription is not None
            and transcription.status is not SpeechToTextStatus.SUCCEEDED
            and wake is None
        )
    if evidence.status is VoiceTurnStatus.INVALID_COMPOSITION:
        if (
            capture_status is not MicrophoneCaptureStatus.SUCCEEDED
            or transcription is None
            or transcription.status is not SpeechToTextStatus.SUCCEEDED
            or not _is_sha256(evidence.capture.audio_sha256)
            or not _is_sha256(transcription.audio_sha256)
            or not _is_sha256(transcription.transcript_sha256)
        ):
            return False
        if wake is None:
            return True
        return (
            _is_sha256(wake.transcript_sha256)
            and wake.transcript_sha256 != transcription.transcript_sha256
        )
    return False


def _is_valid_wake_evidence(evidence: WakeActivationEvidence) -> bool:
    if type(evidence.token_count) is not int or evidence.token_count < 0:
        return False
    if evidence.outcome is WakeActivationOutcome.NOT_DETECTED:
        return (
            evidence.matched_phrase_sha256 is None
            and evidence.match_start_token is None
            and evidence.match_end_token_exclusive is None
        )
    if evidence.outcome is not WakeActivationOutcome.DETECTED:
        return False
    return (
        _is_sha256(evidence.matched_phrase_sha256)
        and type(evidence.match_start_token) is int
        and type(evidence.match_end_token_exclusive) is int
        and 0 <= evidence.match_start_token < evidence.match_end_token_exclusive
        and evidence.match_end_token_exclusive <= evidence.token_count
    )


def _is_public_transcript(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_TRANSCRIPT_CHARS
    ):
        return False
    if any(unicodedata.category(char).startswith("C") for char in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _settle_late_factory_future(
    future: asyncio.Future[VoiceTurnRequest],
    settled: threading.Event,
) -> None:
    try:
        future.exception()
    except asyncio.CancelledError:
        pass
    finally:
        settled.set()
