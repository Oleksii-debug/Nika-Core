from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind

_MAX_ID_UTF8_BYTES = 256
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")


class SpeechAudioFormat(StrEnum):
    WAV = "wav"
    PCM_S16LE = "pcm_s16le"


class SpeechToTextStatus(StrEnum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class SpeechToTextFailureCode(StrEnum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_AUDIO = "invalid_audio"
    RESOURCE_LIMIT = "resource_limit"
    PROVIDER_ERROR = "provider_error"


class SpeechToTextAdapterError(RuntimeError):
    def __init__(
        self,
        code: SpeechToTextFailureCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    data: bytes
    audio_format: SpeechAudioFormat
    sample_rate_hz: int
    channels: int

    def __post_init__(self) -> None:
        if type(self.data) is not bytes or not self.data:
            raise ValueError("audio data must be non-empty bytes")
        if not isinstance(self.audio_format, SpeechAudioFormat):
            raise TypeError("audio_format must be a SpeechAudioFormat")
        if type(self.sample_rate_hz) is not int or not 8_000 <= self.sample_rate_hz <= 192_000:
            raise ValueError("sample_rate_hz must be between 8000 and 192000")
        if type(self.channels) is not int or not 1 <= self.channels <= 8:
            raise ValueError("channels must be between 1 and 8")


@dataclass(frozen=True, slots=True)
class SpeechToTextPolicy:
    max_audio_bytes: int = 32 * 1024 * 1024
    max_transcript_chars: int = 100_000
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        _positive_int(self.max_audio_bytes, "max_audio_bytes")
        _positive_int(self.max_transcript_chars, "max_transcript_chars")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a finite number")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= 3_600.0:
            raise ValueError("timeout_seconds must be in the range (0, 3600]")


@dataclass(frozen=True, slots=True)
class SpeechToTextRequest:
    request_id: str
    provider_id: str
    model: str
    audio: SpeechAudio
    language: str | None = None
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    policy: SpeechToTextPolicy = field(default_factory=SpeechToTextPolicy)

    def __post_init__(self) -> None:
        _bounded_token(self.request_id, "request_id")
        _bounded_token(self.provider_id, "provider_id")
        _bounded_token(self.model, "model")
        if not isinstance(self.audio, SpeechAudio):
            raise TypeError("audio must be SpeechAudio")
        if self.language is not None:
            if type(self.language) is not str or not _LANGUAGE_RE.fullmatch(self.language):
                raise ValueError("language must be a bounded BCP-47-like tag or None")
        if not isinstance(self.privacy, PrivacyClass):
            raise TypeError("privacy must be PrivacyClass")
        if not isinstance(self.policy, SpeechToTextPolicy):
            raise TypeError("policy must be SpeechToTextPolicy")


@dataclass(frozen=True, slots=True)
class SpeechToTextAdapterResponse:
    request_id: str
    provider_id: str
    model: str
    text: str
    detected_language: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class SpeechToTextEvidence:
    request_id: str
    provider_id: str
    model: str
    status: SpeechToTextStatus
    privacy: PrivacyClass
    audio_format: SpeechAudioFormat
    audio_sha256: str | None
    audio_bytes: int
    sample_rate_hz: int
    channels: int
    requested_language: str | None
    detected_language: str | None
    transcript_chars: int | None
    transcript_sha256: str | None
    latency_ms: float | None
    error_code: SpeechToTextFailureCode | None = None
    retryable: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.speech-to-text-evidence:v1",
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "status": self.status.value,
            "privacy": self.privacy.value,
            "audio_format": self.audio_format.value,
            "audio_sha256": self.audio_sha256,
            "audio_bytes": self.audio_bytes,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "requested_language": self.requested_language,
            "detected_language": self.detected_language,
            "transcript_chars": self.transcript_chars,
            "transcript_sha256": self.transcript_sha256,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code.value if self.error_code else None,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class SpeechToTextResult:
    text: str | None
    evidence: SpeechToTextEvidence


class SpeechToTextAdapter(Protocol):
    provider_kind: ProviderKind

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse: ...


class UnavailableSpeechToTextAdapter:
    """Explicit no-STT adapter used when no local speech engine is configured."""

    provider_kind = ProviderKind.LOCAL

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        raise SpeechToTextAdapterError(
            SpeechToTextFailureCode.UNAVAILABLE,
            "local speech-to-text engine is not configured",
            retryable=False,
        )


class SpeechToTextService:
    """Bounded local-only STT boundary with content-free durable evidence.

    Audio bytes and transcript text are transient inputs/outputs. The evidence object contains
    only exact hashes, bounded sizes, route identity and typed outcome. The service deliberately
    has no cloud fallback: future cloud STT requires a separate explicit routing policy rather
    than silently exporting microphone data.
    """

    def __init__(self, adapter: SpeechToTextAdapter) -> None:
        self._adapter = adapter

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextResult:
        if getattr(self._adapter, "provider_kind", None) is not ProviderKind.LOCAL:
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )
        if len(request.audio.data) > request.policy.max_audio_bytes:
            return self._failure(
                request,
                code=SpeechToTextFailureCode.RESOURCE_LIMIT,
                retryable=False,
                include_audio_digest=False,
            )

        try:
            response = await asyncio.wait_for(
                self._adapter.transcribe(request),
                timeout=float(request.policy.timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._failure(
                request,
                code=SpeechToTextFailureCode.TIMEOUT,
                retryable=True,
            )
        except SpeechToTextAdapterError as error:
            status = (
                SpeechToTextStatus.UNAVAILABLE
                if error.code is SpeechToTextFailureCode.UNAVAILABLE
                else SpeechToTextStatus.FAILED
            )
            return self._failure(
                request,
                code=error.code,
                retryable=error.retryable,
                status=status,
            )
        except Exception:
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )

        if not isinstance(response, SpeechToTextAdapterResponse):
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )
        if (
            response.request_id != request.request_id
            or response.provider_id != request.provider_id
            or response.model != request.model
        ):
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )
        if not isinstance(response.text, str) or not response.text.strip():
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )
        if len(response.text) > request.policy.max_transcript_chars:
            return self._failure(
                request,
                code=SpeechToTextFailureCode.RESOURCE_LIMIT,
                retryable=False,
            )
        if response.detected_language is not None:
            if (
                type(response.detected_language) is not str
                or not _LANGUAGE_RE.fullmatch(response.detected_language)
            ):
                return self._failure(
                    request,
                    code=SpeechToTextFailureCode.PROVIDER_ERROR,
                    retryable=False,
                )
        try:
            latency_ms = _validated_latency(response.latency_ms)
        except (TypeError, ValueError):
            return self._failure(
                request,
                code=SpeechToTextFailureCode.PROVIDER_ERROR,
                retryable=False,
            )

        text = response.text
        evidence = self._base_evidence(
            request,
            status=SpeechToTextStatus.SUCCEEDED,
            detected_language=response.detected_language,
            transcript_chars=len(text),
            transcript_sha256=_sha256_bytes(text.encode("utf-8", errors="surrogatepass")),
            latency_ms=latency_ms,
        )
        return SpeechToTextResult(text=text, evidence=evidence)

    def _failure(
        self,
        request: SpeechToTextRequest,
        *,
        code: SpeechToTextFailureCode,
        retryable: bool,
        status: SpeechToTextStatus = SpeechToTextStatus.FAILED,
        include_audio_digest: bool = True,
    ) -> SpeechToTextResult:
        return SpeechToTextResult(
            text=None,
            evidence=self._base_evidence(
                request,
                status=status,
                detected_language=None,
                transcript_chars=None,
                transcript_sha256=None,
                latency_ms=None,
                error_code=code,
                retryable=retryable,
                include_audio_digest=include_audio_digest,
            ),
        )

    def _base_evidence(
        self,
        request: SpeechToTextRequest,
        *,
        status: SpeechToTextStatus,
        detected_language: str | None,
        transcript_chars: int | None,
        transcript_sha256: str | None,
        latency_ms: float | None,
        error_code: SpeechToTextFailureCode | None = None,
        retryable: bool | None = None,
        include_audio_digest: bool = True,
    ) -> SpeechToTextEvidence:
        audio_sha256 = _sha256_bytes(request.audio.data) if include_audio_digest else None
        return SpeechToTextEvidence(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            status=status,
            privacy=request.privacy,
            audio_format=request.audio.audio_format,
            audio_sha256=audio_sha256,
            audio_bytes=len(request.audio.data),
            sample_rate_hz=request.audio.sample_rate_hz,
            channels=request.audio.channels,
            requested_language=request.language,
            detected_language=detected_language,
            transcript_chars=transcript_chars,
            transcript_sha256=transcript_sha256,
            latency_ms=latency_ms,
            error_code=error_code,
            retryable=retryable,
        )


def _bounded_token(value: object, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine token")
    if len(value.encode("utf-8")) > _MAX_ID_UTF8_BYTES:
        raise ValueError(f"{field} exceeds {_MAX_ID_UTF8_BYTES} UTF-8 bytes")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validated_latency(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("latency_ms must be a finite non-negative number or None")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("latency_ms must be a finite non-negative number or None")
    return normalized


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
