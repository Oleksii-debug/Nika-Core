from __future__ import annotations

import asyncio
import io
import re
import wave
from typing import Any

from nika_core.model_gateway.contracts import ProviderKind
from nika_core.speech_to_text import (
    SpeechAudio,
    SpeechAudioFormat,
    SpeechToTextAdapterError,
    SpeechToTextAdapterResponse,
    SpeechToTextFailureCode,
    SpeechToTextPolicy,
    SpeechToTextRequest,
)

_PROVIDER_ID = "sherpa-onnx-whisper"
_MAX_AUDIO_BYTES = 16 * 1024 * 1024
_MAX_PATH_CHARS = 32_767
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")


class SherpaOnnxWhisperSpeechToTextAdapter:
    """Local sherpa-onnx Whisper adapter for the canonical STT boundary."""

    provider_kind = ProviderKind.LOCAL
    provider_id = _PROVIDER_ID

    def __init__(
        self,
        *,
        recognizer: Any,
        model_id: str,
        language: str,
    ) -> None:
        self._recognizer = recognizer
        self._model_id = _bounded_token(model_id, "model_id")
        self._language = _bounded_language(language)
        self.supported_models = (self._model_id,)
        self._inflight: asyncio.Future[str] | None = None

    @classmethod
    def from_whisper_files(
        cls,
        *,
        encoder: str,
        decoder: str,
        tokens: str,
        model_id: str,
        language: str,
        num_threads: int = 2,
        sherpa_module: Any | None = None,
    ) -> SherpaOnnxWhisperSpeechToTextAdapter:
        encoder = _bounded_path(encoder, "encoder")
        decoder = _bounded_path(decoder, "decoder")
        tokens = _bounded_path(tokens, "tokens")
        model_id = _bounded_token(model_id, "model_id")
        language = _bounded_language(language)
        if type(num_threads) is not int or not 1 <= num_threads <= 16:
            raise ValueError("num_threads must be an exact integer in [1, 16]")
        module = sherpa_module if sherpa_module is not None else _load_sherpa_onnx()
        try:
            recognizer = module.OfflineRecognizer.from_whisper(
                encoder=encoder,
                decoder=decoder,
                tokens=tokens,
                language=language,
                task="transcribe",
                num_threads=num_threads,
                debug=False,
                provider="cpu",
            )
        except Exception:  # noqa: BLE001 - native/model initialization boundary
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.UNAVAILABLE,
                "Sherpa ONNX Whisper model initialization failed.",
                retryable=False,
            ) from None
        return cls(recognizer=recognizer, model_id=model_id, language=language)

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        request = _snapshot_request(request)
        if request.provider_id != self.provider_id or request.model != self._model_id:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.PROVIDER_ERROR,
                "Sherpa ONNX STT route does not match configured authority.",
                retryable=False,
            )
        if request.language is not None and request.language != self._language:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.PROVIDER_ERROR,
                "Sherpa ONNX STT language does not match configured authority.",
                retryable=False,
            )
        if request.audio.channels != 1:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.INVALID_AUDIO,
                "Sherpa ONNX STT currently requires mono audio.",
                retryable=False,
            )
        if len(request.audio.data) > _MAX_AUDIO_BYTES:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.RESOURCE_LIMIT,
                "Sherpa ONNX STT audio exceeds the adapter limit.",
                retryable=False,
            )

        self._reap_inflight()
        if self._inflight is not None:
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.RESOURCE_LIMIT,
                "Sherpa ONNX STT decode is still active.",
                retryable=True,
            )

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        future = loop.run_in_executor(None, self._decode_sync, request)
        self._inflight = future
        future.add_done_callback(self._on_decode_done)
        try:
            text = await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except SpeechToTextAdapterError:
            raise
        except Exception:  # noqa: BLE001 - native inference boundary
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.PROVIDER_ERROR,
                "Sherpa ONNX STT decode failed.",
                retryable=True,
            ) from None

        if type(text) is not str or not text.strip():
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.PROVIDER_ERROR,
                "Sherpa ONNX STT returned an invalid transcript.",
                retryable=False,
            )
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text=text.strip(),
            detected_language=None,
            latency_ms=(loop.time() - started_at) * 1000.0,
        )

    def _decode_sync(self, request: SpeechToTextRequest) -> str:
        sample_rate_hz, samples = _normalized_mono_samples(request)
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate_hz, samples)
            self._recognizer.decode_stream(stream)
            result = stream.result
            text = _validated_transcript(result.text)
        except SpeechToTextAdapterError:
            raise
        except Exception:  # noqa: BLE001 - isolate sherpa/onnx/native diagnostics
            raise SpeechToTextAdapterError(
                SpeechToTextFailureCode.PROVIDER_ERROR,
                "Sherpa ONNX STT native decode failed.",
                retryable=True,
            ) from None
        return text

    def _reap_inflight(self) -> None:
        future = self._inflight
        if future is None or not future.done():
            return
        self._inflight = None
        _consume_future(future)

    def _on_decode_done(self, future: asyncio.Future[str]) -> None:
        if self._inflight is future:
            self._inflight = None
        _consume_future(future)


def _normalized_mono_samples(request: SpeechToTextRequest) -> tuple[int, Any]:
    payload = request.audio.data
    sample_rate_hz = request.audio.sample_rate_hz
    if request.audio.audio_format is SpeechAudioFormat.WAV:
        payload, sample_rate_hz = _read_pcm16_wave(request)
    elif request.audio.audio_format is not SpeechAudioFormat.PCM_S16LE:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT audio format is unsupported.",
            retryable=False,
        )
    if not payload or len(payload) % 2:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT PCM16 audio is malformed.",
            retryable=False,
        )

    numpy = _load_numpy()
    try:
        samples = numpy.frombuffer(payload, dtype="<i2").astype(numpy.float32)
        samples *= 1.0 / 32768.0
    except Exception:  # noqa: BLE001 - numerical dependency boundary
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT could not normalize audio.",
            retryable=False,
        ) from None
    if int(samples.size) <= 0:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT audio contains no samples.",
            retryable=False,
        )
    return sample_rate_hz, samples


def _read_pcm16_wave(request: SpeechToTextRequest) -> tuple[bytes, int]:
    try:
        with wave.open(io.BytesIO(request.audio.data), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate_hz = wav.getframerate()
            compression = wav.getcomptype()
            frame_count = wav.getnframes()
            payload = wav.readframes(frame_count)
    except (EOFError, wave.Error):
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT WAV audio is malformed.",
            retryable=False,
        ) from None
    if (
        channels != 1
        or channels != request.audio.channels
        or sample_width != 2
        or sample_rate_hz != request.audio.sample_rate_hz
        or compression != "NONE"
    ):
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.INVALID_AUDIO,
            "Sherpa ONNX STT WAV metadata does not match the request.",
            retryable=False,
        )
    if not payload or len(payload) > _MAX_AUDIO_BYTES:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.RESOURCE_LIMIT,
            "Sherpa ONNX STT WAV payload exceeds the adapter limit.",
            retryable=False,
        )
    return payload, sample_rate_hz


def _snapshot_request(request: SpeechToTextRequest) -> SpeechToTextRequest:
    if type(request) is not SpeechToTextRequest:
        raise TypeError("request must be an exact SpeechToTextRequest")
    if type(request.audio) is not SpeechAudio:
        raise TypeError("request.audio must be an exact SpeechAudio")
    if type(request.policy) is not SpeechToTextPolicy:
        raise TypeError("request.policy must be an exact SpeechToTextPolicy")
    return SpeechToTextRequest(
        request_id=request.request_id,
        provider_id=request.provider_id,
        model=request.model,
        audio=SpeechAudio(
            data=request.audio.data,
            audio_format=request.audio.audio_format,
            sample_rate_hz=request.audio.sample_rate_hz,
            channels=request.audio.channels,
        ),
        language=request.language,
        privacy=request.privacy,
        policy=SpeechToTextPolicy(
            max_audio_bytes=request.policy.max_audio_bytes,
            max_transcript_chars=request.policy.max_transcript_chars,
            timeout_seconds=request.policy.timeout_seconds,
        ),
    )


def _load_sherpa_onnx() -> Any:
    try:
        import sherpa_onnx  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - dependency/native-load boundary
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.UNAVAILABLE,
            "Sherpa ONNX STT dependency is unavailable.",
            retryable=False,
        ) from None
    return sherpa_onnx


def _load_numpy() -> Any:
    try:
        import numpy  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - dependency/native-load boundary
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.UNAVAILABLE,
            "Sherpa ONNX STT numerical dependency is unavailable.",
            retryable=False,
        ) from None
    return numpy


def _bounded_token(value: object, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine token")
    return value


def _bounded_language(value: object) -> str:
    if type(value) is not str or not _LANGUAGE_RE.fullmatch(value):
        raise ValueError("language must be a bounded BCP-47-like tag")
    return value


def _bounded_path(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_PATH_CHARS
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or value.startswith(("\\\\", "//"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) is not None
    ):
        raise ValueError(f"{field} must be a bounded local path")
    return value


def _validated_transcript(value: object) -> str:
    if type(value) is not str:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.PROVIDER_ERROR,
            "Sherpa ONNX STT result text is invalid.",
            retryable=False,
        )
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.PROVIDER_ERROR,
            "Sherpa ONNX STT result text is invalid.",
            retryable=False,
        )
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.PROVIDER_ERROR,
            "Sherpa ONNX STT result text is invalid.",
            retryable=False,
        ) from None
    return normalized


def _consume_future(future: asyncio.Future[str]) -> None:
    try:
        future.result()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - late native decode result is intentionally discarded
        return
