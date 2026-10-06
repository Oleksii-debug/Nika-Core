from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nika_core.microphone_capture import (
    MicrophoneCapturePolicy,
    MicrophoneCaptureRequest,
    MicrophoneCaptureService,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.sherpa_onnx_stt import SherpaOnnxWhisperSpeechToTextAdapter
from nika_core.speech_to_text import (
    SpeechToTextAdapterError,
    SpeechToTextAdapterResponse,
    SpeechToTextFailureCode,
    SpeechToTextPolicy,
    SpeechToTextRequest,
    SpeechToTextService,
)
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_voice import DesktopVoiceTurnController, VoiceSubmitter
from nika_core.voice_turn import OneShotVoiceTurnService, VoiceTurnRequest
from nika_core.wake_activation import MAX_TRANSCRIPT_CHARS, WakeActivationDetector
from nika_core.windows_microphone_capture import WindowsWasapiMicrophoneCaptureAdapter

_SAMPLE_RATE_HZ = 16_000
_CAPTURE_SECONDS = 8
_CAPTURE_BYTES = _SAMPLE_RATE_HZ * _CAPTURE_SECONDS * 2
_MODEL_ID = "whisper-local"
_LANGUAGE = "uk"
_MODEL_DIR = Path("voice") / "whisper"


class _LazySherpaAdapter:
    """Load local Whisper only after an explicit packaged voice turn starts."""

    provider_kind = ProviderKind.LOCAL
    provider_id = "sherpa-onnx-whisper"
    supported_models = (_MODEL_ID,)

    def __init__(self, *, encoder: Path, decoder: Path, tokens: Path) -> None:
        self._encoder = encoder
        self._decoder = decoder
        self._tokens = tokens
        self._delegate: SherpaOnnxWhisperSpeechToTextAdapter | None = None
        self._load_future: asyncio.Future[Any] | None = None

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        delegate = self._delegate
        if delegate is None:
            loop = asyncio.get_running_loop()
            future = self._load_future
            if future is None:
                future = loop.run_in_executor(None, self._load_sync)
                self._load_future = future
                future.add_done_callback(self._on_load_done)
            try:
                delegate = await asyncio.shield(future)
            except asyncio.CancelledError:
                raise
            except SpeechToTextAdapterError:
                raise
            except Exception:  # noqa: BLE001 - local native/model initialization boundary
                raise SpeechToTextAdapterError(
                    SpeechToTextFailureCode.UNAVAILABLE,
                    "Sherpa ONNX Whisper model initialization failed.",
                    retryable=False,
                ) from None
        return await delegate.transcribe(request)

    def _load_sync(self) -> SherpaOnnxWhisperSpeechToTextAdapter:
        return SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
            encoder=str(self._encoder),
            decoder=str(self._decoder),
            tokens=str(self._tokens),
            model_id=_MODEL_ID,
            language=_LANGUAGE,
            num_threads=2,
        )

    def _on_load_done(
        self,
        future: asyncio.Future[SherpaOnnxWhisperSpeechToTextAdapter],
    ) -> None:
        if self._load_future is not future:
            return
        self._load_future = None
        if future.cancelled():
            return
        try:
            delegate = future.result()
        except Exception:  # noqa: BLE001 - late native/model initialization result
            return
        self._delegate = delegate


class PackagedVoiceFeature:
    """Bounded packaged facade over the canonical one-shot voice controller."""

    def __init__(
        self,
        *,
        controller: DesktopVoiceTurnController | None,
        unavailable_message: str | None = None,
    ) -> None:
        if controller is None:
            if type(unavailable_message) is not str or not unavailable_message:
                raise ValueError("unavailable packaged voice requires a bounded message")
        else:
            if type(controller) is not DesktopVoiceTurnController:
                raise TypeError("controller must be an exact DesktopVoiceTurnController")
            if unavailable_message is not None:
                raise ValueError("available packaged voice must not carry unavailable text")
        self._controller = controller
        self._unavailable_message = unavailable_message
        self._closed = False

    @property
    def available(self) -> bool:
        return self._controller is not None

    def start(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        if self._closed:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message="Голосовий ввід уже завершив роботу разом із застосунком.",
                focus_id="voice-heading",
            )
        if self._controller is None:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=self._unavailable_message or "Голосовий ввід недоступний.",
                focus_id="voice-heading",
            )
        try:
            return self._controller.start(payload)
        except RuntimeError:
            return UIResult(
                request_id="desktop-handler",
                status="failed",
                message="Не вдалося запустити голосовий ввід.",
                focus_id="voice-heading",
            )

    def cancel(self, payload: Mapping[str, Any]) -> UIResult:
        self._require_empty_payload(payload)
        if self._closed:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Активного голосового вводу немає.",
                focus_id="voice-heading",
            )
        if self._controller is None:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Активного голосового вводу немає.",
                focus_id="voice-heading",
            )
        return self._controller.cancel(payload)

    def snapshot(self) -> dict[str, object]:
        if self._controller is None:
            return {
                "schema": "nika.packaged-voice-state:v1",
                "available": False,
                "message": self._unavailable_message,
                "turn": None,
            }
        return {
            "schema": "nika.packaged-voice-state:v1",
            "available": True,
            "message": (
                "Голосовий ввід працює локально. Розпізнаний текст не запускає "
                "завдання без окремого підтвердження."
            ),
            "turn": self._controller.snapshot(),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._controller is None:
            return
        self._controller.close()

    @staticmethod
    def _require_empty_payload(payload: Mapping[str, Any]) -> None:
        if type(payload) is not dict:
            raise TypeError("packaged voice action payload must be an exact dict")
        if payload:
            raise ValueError("packaged voice action does not accept payload authority")


def build_packaged_voice(
    data_root: Path,
    *,
    submit: VoiceSubmitter | None = None,
) -> PackagedVoiceFeature:
    """Build packaged one-shot voice from local model files, with no download fallback."""

    if not isinstance(data_root, Path):
        raise TypeError("data_root must be a pathlib.Path")
    root = data_root.expanduser()
    if not root.is_absolute():
        raise ValueError("data_root must be absolute")
    if sys.platform != "win32":
        return PackagedVoiceFeature(
            controller=None,
            unavailable_message="Голосовий ввід доступний лише у застосунку Windows.",
        )

    model_root = root / _MODEL_DIR
    encoder = model_root / "encoder.onnx"
    decoder = model_root / "decoder.onnx"
    tokens = model_root / "tokens.txt"
    if not all(path.is_file() for path in (encoder, decoder, tokens)):
        return PackagedVoiceFeature(
            controller=None,
            unavailable_message=(
                "Локальна голосова модель не встановлена. Додайте encoder.onnx, "
                "decoder.onnx і tokens.txt до папки NikaCore\\voice\\whisper."
            ),
        )

    if submit is None or not callable(submit):
        raise TypeError("available packaged voice requires a callable desktop submitter")

    microphone = WindowsWasapiMicrophoneCaptureAdapter()
    stt = _LazySherpaAdapter(
        encoder=encoder,
        decoder=decoder,
        tokens=tokens,
    )

    service = OneShotVoiceTurnService(
        microphone=MicrophoneCaptureService(microphone),
        speech_to_text=SpeechToTextService(stt),
        wake_detector=WakeActivationDetector(),
        enable_voice_activity=True,
    )
    def request_factory(request_id: str) -> VoiceTurnRequest:
        capabilities = microphone.capabilities
        return VoiceTurnRequest(
            request_id=request_id,
            capture=MicrophoneCaptureRequest(
                request_id=request_id,
                provider_id=capabilities.provider_id,
                device_id=capabilities.device_id,
                sample_rate_hz=_SAMPLE_RATE_HZ,
                sample_count=_SAMPLE_RATE_HZ * _CAPTURE_SECONDS,
                policy=MicrophoneCapturePolicy(
                    max_audio_bytes=_CAPTURE_BYTES,
                    timeout_seconds=12.0,
                ),
            ),
            stt_provider_id=stt.provider_id,
            stt_model=_MODEL_ID,
            language=_LANGUAGE,
            stt_policy=SpeechToTextPolicy(
                max_audio_bytes=_CAPTURE_BYTES,
                max_transcript_chars=MAX_TRANSCRIPT_CHARS,
                timeout_seconds=30.0,
            ),
        )

    return PackagedVoiceFeature(
        controller=DesktopVoiceTurnController(
            service=service,
            request_factory=request_factory,
            submit=submit,
        ),
    )
