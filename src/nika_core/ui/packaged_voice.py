from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from collections.abc import Callable, Mapping
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
from nika_core.ui.packaged_voice_model_setup import PackagedVoiceModelSetup
from nika_core.voice_turn import OneShotVoiceTurnService, VoiceTurnRequest
from nika_core.wake_activation import MAX_TRANSCRIPT_CHARS, WakeActivationDetector
from nika_core.windows_microphone_capture import WindowsWasapiMicrophoneCaptureAdapter

_SAMPLE_RATE_HZ = 16_000
_CAPTURE_SECONDS = 8
_CAPTURE_BYTES = _SAMPLE_RATE_HZ * _CAPTURE_SECONDS * 2
_MODEL_ID = "whisper-local"
_LANGUAGE = "uk"
_MODEL_DIR = Path("voice") / "whisper"
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_REPARSE_POINT = 0x400

_ModelIdentity = tuple[int, int, int, int, int, int]
_ModelAuthorityEntry = tuple[Path, int, _ModelIdentity]


class _ModelAuthorityError(RuntimeError):
    pass


def _is_reparse(evidence: os.stat_result) -> bool:
    attributes = int(getattr(evidence, "st_file_attributes", 0))
    return bool(attributes & _REPARSE_POINT)


def _model_identity(evidence: os.stat_result) -> _ModelIdentity:
    return (
        int(evidence.st_dev),
        int(evidence.st_ino),
        int(evidence.st_size),
        int(evidence.st_mtime_ns),
        int(evidence.st_ctime_ns),
        int(evidence.st_nlink),
    )


def _require_model_file(path: Path) -> tuple[os.stat_result, _ModelIdentity]:
    try:
        evidence = os.lstat(path)
    except OSError as exc:
        raise _ModelAuthorityError("voice model file is unavailable") from exc
    if (
        not stat.S_ISREG(evidence.st_mode)
        or stat.S_ISLNK(evidence.st_mode)
        or _is_reparse(evidence)
        or evidence.st_size <= 0
        or evidence.st_nlink != 1
    ):
        raise _ModelAuthorityError("voice model file is not a direct regular file")
    return evidence, _model_identity(evidence)


def _open_model_authority(path: Path) -> int:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
        except ImportError as exc:
            raise OSError("Windows model authority support is unavailable") from exc

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(path),
            _WINDOWS_GENERIC_READ,
            _WINDOWS_FILE_SHARE_READ,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            return msvcrt.open_osfhandle(
                int(handle),
                os.O_RDONLY | int(getattr(os, "O_BINARY", 0)),
            )
        except (OSError, OverflowError, ValueError):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    return os.open(path, flags)


class _PinnedModelAuthority:
    """Keep admitted model files immutable for the packaged voice feature lifetime."""

    def __init__(self, paths: tuple[Path, Path, Path]) -> None:
        self._lock = threading.Lock()
        self._entries: tuple[_ModelAuthorityEntry, ...] = ()
        self._consuming = False
        self._close_requested = False
        self._closed = False
        opened: list[_ModelAuthorityEntry] = []
        try:
            for path in paths:
                _before, expected = _require_model_file(path)
                descriptor = _open_model_authority(path)
                try:
                    opened_stat = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened_stat.st_mode)
                        or _is_reparse(opened_stat)
                        or _model_identity(opened_stat) != expected
                    ):
                        raise _ModelAuthorityError(
                            "voice model file changed while authority was acquired"
                        )
                except Exception:
                    os.close(descriptor)
                    raise
                opened.append((path, descriptor, expected))
            self._entries = tuple(opened)
            self._verify_locked()
        except Exception:
            for _path, descriptor, _expected in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def run(self, loader: Callable[[], Any]) -> Any:
        with self._lock:
            if self._closed:
                raise _ModelAuthorityError("voice model authority is closed")
            if self._consuming:
                raise _ModelAuthorityError("voice model authority is already in use")
            try:
                self._verify_locked()
            except _ModelAuthorityError:
                self._close_locked()
                raise
            self._consuming = True

        poisoned = False
        try:
            result = loader()
            with self._lock:
                self._verify_locked()
            return result
        except _ModelAuthorityError:
            poisoned = True
            raise
        finally:
            with self._lock:
                self._consuming = False
                if poisoned or self._close_requested:
                    self._close_locked()

    def close(self) -> None:
        with self._lock:
            self._close_requested = True
            if not self._consuming:
                self._close_locked()

    def _verify_locked(self) -> None:
        if self._closed or len(self._entries) != 3:
            raise _ModelAuthorityError("voice model authority is unavailable")
        for path, descriptor, expected in self._entries:
            try:
                opened = os.fstat(descriptor)
                _current_stat, current = _require_model_file(path)
            except OSError as exc:
                raise _ModelAuthorityError("voice model authority changed") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse(opened)
                or _model_identity(opened) != expected
                or current != expected
            ):
                raise _ModelAuthorityError("voice model authority changed")

    def _close_locked(self) -> None:
        if self._closed:
            return
        entries = self._entries
        self._entries = ()
        self._closed = True
        for _path, descriptor, _expected in entries:
            try:
                os.close(descriptor)
            except OSError:
                pass


class _LazySherpaAdapter:
    """Load local Whisper only after an explicit packaged voice turn starts."""

    provider_kind = ProviderKind.LOCAL
    provider_id = "sherpa-onnx-whisper"
    supported_models = (_MODEL_ID,)

    def __init__(self, *, encoder: Path, decoder: Path, tokens: Path) -> None:
        self._encoder = encoder
        self._decoder = decoder
        self._tokens = tokens
        self._authority = _PinnedModelAuthority((encoder, decoder, tokens))
        self._delegate: SherpaOnnxWhisperSpeechToTextAdapter | None = None
        self._load_future: asyncio.Future[Any] | None = None
        self._closed = False

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        if self._closed:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.UNAVAILABLE,
                "Sherpa ONNX Whisper model authority is closed.",
                retryable=False,
            )
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
        def load() -> SherpaOnnxWhisperSpeechToTextAdapter:
            return SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
                encoder=str(self._encoder),
                decoder=str(self._decoder),
                tokens=str(self._tokens),
                model_id=_MODEL_ID,
                language=_LANGUAGE,
                num_threads=2,
            )

        return self._authority.run(load)

    def close(self) -> None:
        self._closed = True
        self._authority.close()

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
        if self._closed:
            return
        self._delegate = delegate


class PackagedVoiceFeature:
    """Bounded packaged facade over the canonical one-shot voice controller."""

    def __init__(
        self,
        *,
        controller: DesktopVoiceTurnController | None,
        model_loader: _LazySherpaAdapter | None = None,
        unavailable_message: str | None = None,
    ) -> None:
        if controller is None:
            if type(unavailable_message) is not str or not unavailable_message:
                raise ValueError("unavailable packaged voice requires a bounded message")
            if model_loader is not None:
                raise ValueError("unavailable packaged voice must not retain model authority")
        else:
            if type(controller) is not DesktopVoiceTurnController:
                raise TypeError("controller must be an exact DesktopVoiceTurnController")
            if type(model_loader) is not _LazySherpaAdapter:
                raise TypeError("available packaged voice requires its exact model loader")
            if unavailable_message is not None:
                raise ValueError("available packaged voice must not carry unavailable text")
        self._controller = controller
        self._model_loader = model_loader
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
        try:
            self._controller.close()
        finally:
            if self._model_loader is not None:
                self._model_loader.close()

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

    installation = PackagedVoiceModelSetup(root).snapshot()
    if installation.get("status") != "installed":
        return PackagedVoiceFeature(
            controller=None,
            unavailable_message=(
                "Локальна голосова модель не встановлена безпечно. "
                "Скористайтеся розділом імпорту локальної голосової моделі."
            ),
        )

    model_root = root / _MODEL_DIR
    encoder = model_root / "encoder.onnx"
    decoder = model_root / "decoder.onnx"
    tokens = model_root / "tokens.txt"

    if submit is None or not callable(submit):
        raise TypeError("available packaged voice requires a callable desktop submitter")

    try:
        stt = _LazySherpaAdapter(
            encoder=encoder,
            decoder=decoder,
            tokens=tokens,
        )
    except (OSError, RuntimeError):
        return PackagedVoiceFeature(
            controller=None,
            unavailable_message=(
                "Локальна голосова модель змінилася або недоступна для безпечного "
                "локального завантаження. Перезапустіть Nika Core після перевірки моделі."
            ),
        )

    microphone = WindowsWasapiMicrophoneCaptureAdapter()
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
        model_loader=stt,
    )
