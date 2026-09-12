from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_MIN_SAMPLE_RATE_HZ = 8_000
_MAX_SAMPLE_RATE_HZ = 48_000
_MAX_CAPTURE_SECONDS = 30
_MAX_AUDIO_BYTES = 8 * 1024 * 1024
_MAX_ID_BYTES = 256
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")


class MicrophoneCaptureStatus(StrEnum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class MicrophoneCaptureFailureCode(StrEnum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    ADAPTER_ERROR = "adapter_error"
    ROUTE_MISMATCH = "route_mismatch"
    INVALID_RESPONSE = "invalid_response"


class MicrophoneCaptureAdapterError(RuntimeError):
    def __init__(
        self,
        code: MicrophoneCaptureFailureCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        if type(code) is not MicrophoneCaptureFailureCode:
            raise TypeError("code must be a MicrophoneCaptureFailureCode")
        if type(message) is not str:
            raise TypeError("message must be an exact string")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureCapabilities:
    provider_id: str
    device_id: str
    min_sample_rate_hz: int = _MIN_SAMPLE_RATE_HZ
    max_sample_rate_hz: int = _MAX_SAMPLE_RATE_HZ

    def __post_init__(self) -> None:
        _bounded_token(self.provider_id, field="provider_id")
        _bounded_token(self.device_id, field="device_id")
        _bounded_int(
            self.min_sample_rate_hz,
            field="min_sample_rate_hz",
            minimum=_MIN_SAMPLE_RATE_HZ,
            maximum=_MAX_SAMPLE_RATE_HZ,
        )
        _bounded_int(
            self.max_sample_rate_hz,
            field="max_sample_rate_hz",
            minimum=_MIN_SAMPLE_RATE_HZ,
            maximum=_MAX_SAMPLE_RATE_HZ,
        )
        if self.min_sample_rate_hz > self.max_sample_rate_hz:
            raise ValueError("minimum sample rate must not exceed maximum sample rate")


@dataclass(frozen=True, slots=True)
class MicrophoneCapturePolicy:
    max_audio_bytes: int = 2 * 1024 * 1024
    timeout_seconds: float = 35.0

    def __post_init__(self) -> None:
        _bounded_int(
            self.max_audio_bytes,
            field="max_audio_bytes",
            minimum=2,
            maximum=_MAX_AUDIO_BYTES,
        )
        _finite_float(
            self.timeout_seconds,
            field="timeout_seconds",
            minimum_exclusive=0.0,
            maximum=120.0,
        )


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureRequest:
    request_id: str
    provider_id: str
    device_id: str
    sample_rate_hz: int
    sample_count: int
    policy: MicrophoneCapturePolicy = field(default_factory=MicrophoneCapturePolicy)

    def __post_init__(self) -> None:
        _bounded_token(self.request_id, field="request_id")
        _bounded_token(self.provider_id, field="provider_id")
        _bounded_token(self.device_id, field="device_id")
        _bounded_int(
            self.sample_rate_hz,
            field="sample_rate_hz",
            minimum=_MIN_SAMPLE_RATE_HZ,
            maximum=_MAX_SAMPLE_RATE_HZ,
        )
        _bounded_int(
            self.sample_count,
            field="sample_count",
            minimum=1,
            maximum=self.sample_rate_hz * _MAX_CAPTURE_SECONDS,
        )
        if type(self.policy) is not MicrophoneCapturePolicy:
            raise TypeError("policy must be an exact MicrophoneCapturePolicy")

    @property
    def expected_audio_bytes(self) -> int:
        return self.sample_count * 2


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureResponse:
    request_id: str
    provider_id: str
    device_id: str
    sample_rate_hz: int
    pcm_s16le: bytes
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureEvidence:
    request_id: str
    provider_id: str
    device_id_sha256: str
    status: MicrophoneCaptureStatus
    sample_rate_hz: int
    sample_count: int
    audio_byte_count: int
    audio_sha256: str | None
    latency_ms: float | None
    error_code: MicrophoneCaptureFailureCode | None = None
    retryable: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.microphone-capture-evidence:v1",
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "device_id_sha256": self.device_id_sha256,
            "status": self.status.value,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": self.sample_count,
            "audio_byte_count": self.audio_byte_count,
            "audio_sha256": self.audio_sha256,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code.value if self.error_code else None,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureResult:
    pcm_s16le: bytes | None
    evidence: MicrophoneCaptureEvidence


class MicrophoneCaptureAdapter(Protocol):
    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities: ...

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse: ...


class UnavailableMicrophoneCaptureAdapter:
    def __init__(
        self,
        *,
        provider_id: str = "microphone-unavailable",
        device_id: str = "unavailable",
    ) -> None:
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id=provider_id,
            device_id=device_id,
        )

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        del request
        raise MicrophoneCaptureAdapterError(
            MicrophoneCaptureFailureCode.UNAVAILABLE,
            "local microphone capture is not configured",
            retryable=False,
        )


class MicrophoneCaptureService:
    """Bounded transient local microphone-capture boundary.

    The service returns PCM16 bytes only to the immediate caller. Reportable evidence contains
    hashes and bounded metadata, never raw audio or the raw logical device identifier.
    """

    def __init__(self, adapter: MicrophoneCaptureAdapter) -> None:
        self._adapter = adapter

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResult:
        if type(request) is not MicrophoneCaptureRequest:
            raise TypeError("request must be an exact MicrophoneCaptureRequest")

        try:
            before = self._read_capabilities()
        except MicrophoneCaptureAdapterError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )
        route_error = self._validate_route(request, before)
        if route_error is not None:
            return self._failure(
                request,
                code=route_error,
                retryable=False,
            )

        if request.expected_audio_bytes > request.policy.max_audio_bytes:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.RESOURCE_LIMIT,
                retryable=False,
            )

        try:
            response = await asyncio.wait_for(
                self._adapter.capture(request),
                timeout=float(request.policy.timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.TIMEOUT,
                retryable=True,
            )
        except MicrophoneCaptureAdapterError as error:
            status = (
                MicrophoneCaptureStatus.UNAVAILABLE
                if error.code is MicrophoneCaptureFailureCode.UNAVAILABLE
                else MicrophoneCaptureStatus.FAILED
            )
            return self._failure(
                request,
                code=error.code,
                retryable=error.retryable,
                status=status,
            )
        except Exception:  # noqa: BLE001 - adapter trust boundary
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )

        try:
            after = self._read_capabilities()
        except MicrophoneCaptureAdapterError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )
        if after != before:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ROUTE_MISMATCH,
                retryable=False,
            )
        if type(response) is not MicrophoneCaptureResponse:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.INVALID_RESPONSE,
                retryable=False,
            )
        if not self._response_identity_matches(request, response):
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ROUTE_MISMATCH,
                retryable=False,
            )
        if type(response.pcm_s16le) is not bytes:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.INVALID_RESPONSE,
                retryable=False,
            )
        if len(response.pcm_s16le) != request.expected_audio_bytes:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.INVALID_RESPONSE,
                retryable=False,
            )
        try:
            latency_ms = _validated_latency(response.latency_ms)
        except (TypeError, ValueError):
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.INVALID_RESPONSE,
                retryable=False,
            )

        audio = response.pcm_s16le
        evidence = MicrophoneCaptureEvidence(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id_sha256=_sha256_text(request.device_id),
            status=MicrophoneCaptureStatus.SUCCEEDED,
            sample_rate_hz=request.sample_rate_hz,
            sample_count=request.sample_count,
            audio_byte_count=len(audio),
            audio_sha256=hashlib.sha256(audio).hexdigest(),
            latency_ms=latency_ms,
        )
        return MicrophoneCaptureResult(pcm_s16le=audio, evidence=evidence)

    def _read_capabilities(self) -> MicrophoneCaptureCapabilities:
        try:
            capabilities = self._adapter.capabilities
        except Exception as error:  # noqa: BLE001 - adapter trust boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "microphone capabilities are unavailable",
                retryable=False,
            ) from error
        if type(capabilities) is not MicrophoneCaptureCapabilities:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "microphone capabilities are invalid",
                retryable=False,
            )
        return capabilities

    @staticmethod
    def _validate_route(
        request: MicrophoneCaptureRequest,
        capabilities: MicrophoneCaptureCapabilities,
    ) -> MicrophoneCaptureFailureCode | None:
        if (
            request.provider_id != capabilities.provider_id
            or request.device_id != capabilities.device_id
        ):
            return MicrophoneCaptureFailureCode.ROUTE_MISMATCH
        if not (
            capabilities.min_sample_rate_hz
            <= request.sample_rate_hz
            <= capabilities.max_sample_rate_hz
        ):
            return MicrophoneCaptureFailureCode.RESOURCE_LIMIT
        return None

    @staticmethod
    def _response_identity_matches(
        request: MicrophoneCaptureRequest,
        response: MicrophoneCaptureResponse,
    ) -> bool:
        try:
            response_request_id = _bounded_token(response.request_id, field="response.request_id")
            response_provider_id = _bounded_token(
                response.provider_id,
                field="response.provider_id",
            )
            response_device_id = _bounded_token(response.device_id, field="response.device_id")
            response_sample_rate = _bounded_int(
                response.sample_rate_hz,
                field="response.sample_rate_hz",
                minimum=_MIN_SAMPLE_RATE_HZ,
                maximum=_MAX_SAMPLE_RATE_HZ,
            )
        except (TypeError, ValueError):
            return False
        return (
            response_request_id == request.request_id
            and response_provider_id == request.provider_id
            and response_device_id == request.device_id
            and response_sample_rate == request.sample_rate_hz
        )

    @staticmethod
    def _failure(
        request: MicrophoneCaptureRequest,
        *,
        code: MicrophoneCaptureFailureCode,
        retryable: bool,
        status: MicrophoneCaptureStatus = MicrophoneCaptureStatus.FAILED,
    ) -> MicrophoneCaptureResult:
        evidence = MicrophoneCaptureEvidence(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id_sha256=_sha256_text(request.device_id),
            status=status,
            sample_rate_hz=request.sample_rate_hz,
            sample_count=request.sample_count,
            audio_byte_count=0,
            audio_sha256=None,
            latency_ms=None,
            error_code=code,
            retryable=retryable,
        )
        return MicrophoneCaptureResult(pcm_s16le=None, evidence=evidence)


def _bounded_token(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if len(value.encode("utf-8")) > _MAX_ID_BYTES or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine token")
    return value


def _bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an exact integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} is outside the supported bound")
    return value


def _finite_float(
    value: object,
    *,
    field: str,
    minimum_exclusive: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite number")
    try:
        converted = float(value)
    except OverflowError as error:
        raise ValueError(f"{field} is outside the supported bound") from error
    if not math.isfinite(converted) or not minimum_exclusive < converted <= maximum:
        raise ValueError(f"{field} is outside the supported bound")
    return converted


def _validated_latency(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("latency_ms must be a finite non-negative number")
    try:
        converted = float(value)
    except OverflowError as error:
        raise ValueError("latency_ms is outside the supported bound") from error
    if not math.isfinite(converted) or not 0.0 <= converted <= 3_600_000.0:
        raise ValueError("latency_ms is outside the supported bound")
    return converted


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
