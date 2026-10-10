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
    CLEANUP_PENDING = "cleanup_pending"
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
        timeout_seconds = _finite_float(
            self.timeout_seconds,
            field="timeout_seconds",
            minimum_exclusive=0.0,
            maximum=120.0,
        )
        object.__setattr__(self, "timeout_seconds", timeout_seconds)


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
        object.__setattr__(self, "policy", _snapshot_policy(self.policy))

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
    cleanup_pending: bool = False

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
            "cleanup_pending": self.cleanup_pending,
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
        self._capture_lock = asyncio.Lock()
        self._cleanup_futures: set[asyncio.Future[object]] = set()

    @property
    def cleanup_pending(self) -> bool:
        self._reap_cleanup_futures()
        return bool(self._cleanup_futures)

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResult:
        request = _snapshot_request(request)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.policy.timeout_seconds
        try:
            await asyncio.wait_for(
                self._capture_lock.acquire(),
                timeout=max(0.0, deadline - loop.time()),
            )
        except TimeoutError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.TIMEOUT,
                retryable=True,
            )
        try:
            return await self._capture_serialized(request, deadline)
        finally:
            self._capture_lock.release()

    async def _capture_serialized(
        self,
        request: MicrophoneCaptureRequest,
        deadline: float,
    ) -> MicrophoneCaptureResult:
        if self.cleanup_pending:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.CLEANUP_PENDING,
                retryable=True,
                cleanup_pending=True,
            )

        loop = asyncio.get_running_loop()

        if request.expected_audio_bytes > request.policy.max_audio_bytes:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.RESOURCE_LIMIT,
                retryable=False,
            )

        try:
            before = await self._read_capabilities_bounded(deadline)
        except MicrophoneCaptureAdapterError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )
        if before is None:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.TIMEOUT,
                retryable=True,
                cleanup_pending=self.cleanup_pending,
            )
        route_error = self._validate_route(request, before)
        if route_error is not None:
            return self._failure(
                request,
                code=route_error,
                retryable=False,
            )

        adapter_request = _snapshot_request(request)
        capture_task = asyncio.create_task(self._adapter.capture(adapter_request))
        try:
            done, _ = await asyncio.wait(
                {capture_task},
                timeout=max(0.0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            capture_task.cancel()
            self._track_cleanup(capture_task)
            raise

        if capture_task not in done or loop.time() >= deadline:
            capture_task.cancel()
            cleanup_pending = not capture_task.done()
            self._track_cleanup(capture_task)
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.TIMEOUT,
                retryable=True,
                cleanup_pending=cleanup_pending,
            )

        try:
            response = capture_task.result()
        except asyncio.CancelledError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )
        except MicrophoneCaptureAdapterError as error:
            code, retryable = _normalized_adapter_error(error)
            status = (
                MicrophoneCaptureStatus.UNAVAILABLE
                if code is MicrophoneCaptureFailureCode.UNAVAILABLE
                else MicrophoneCaptureStatus.FAILED
            )
            return self._failure(
                request,
                code=code,
                retryable=retryable,
                status=status,
            )
        except Exception:  # noqa: BLE001 - adapter trust boundary
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )

        try:
            after = await self._read_capabilities_bounded(deadline)
        except MicrophoneCaptureAdapterError:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                retryable=False,
            )
        if after is None:
            return self._failure(
                request,
                code=MicrophoneCaptureFailureCode.TIMEOUT,
                retryable=True,
                cleanup_pending=self.cleanup_pending,
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

    def _discard_or_track_cleanup(self, future: asyncio.Future[object]) -> None:
        if future.done():
            _consume_future_result(future)
            return
        self._track_cleanup(future)

    def _track_cleanup(self, future: asyncio.Future[object]) -> None:
        self._cleanup_futures.add(future)
        future.add_done_callback(self._on_cleanup_done)

    def _on_cleanup_done(self, future: asyncio.Future[object]) -> None:
        self._cleanup_futures.discard(future)
        _consume_future_result(future)

    def _reap_cleanup_futures(self) -> None:
        done = tuple(future for future in self._cleanup_futures if future.done())
        for future in done:
            self._cleanup_futures.discard(future)
            _consume_future_result(future)

    async def _read_capabilities_bounded(
        self,
        deadline: float,
    ) -> MicrophoneCaptureCapabilities | None:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0.0:
            return None
        future = loop.run_in_executor(None, self._read_capabilities)
        try:
            done, _ = await asyncio.wait(
                {future},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            self._discard_or_track_cleanup(future)
            raise
        if future not in done or loop.time() >= deadline:
            self._discard_or_track_cleanup(future)
            return None
        return future.result()

    def _read_capabilities(self) -> MicrophoneCaptureCapabilities:
        try:
            capabilities = self._adapter.capabilities
        except Exception:  # noqa: BLE001 - adapter capability boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "microphone capabilities are unavailable",
                retryable=False,
            ) from None
        try:
            return _snapshot_capabilities(capabilities)
        except (AttributeError, TypeError, ValueError):
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "microphone capabilities are invalid",
                retryable=False,
            ) from None

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
        cleanup_pending: bool = False,
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
            cleanup_pending=cleanup_pending,
        )
        return MicrophoneCaptureResult(pcm_s16le=None, evidence=evidence)


def _snapshot_capabilities(value: object) -> MicrophoneCaptureCapabilities:
    if type(value) is not MicrophoneCaptureCapabilities:
        raise TypeError("capabilities must be an exact MicrophoneCaptureCapabilities")
    return MicrophoneCaptureCapabilities(
        provider_id=value.provider_id,
        device_id=value.device_id,
        min_sample_rate_hz=value.min_sample_rate_hz,
        max_sample_rate_hz=value.max_sample_rate_hz,
    )


def _snapshot_policy(value: object) -> MicrophoneCapturePolicy:
    if type(value) is not MicrophoneCapturePolicy:
        raise TypeError("policy must be an exact MicrophoneCapturePolicy")
    return MicrophoneCapturePolicy(
        max_audio_bytes=value.max_audio_bytes,
        timeout_seconds=value.timeout_seconds,
    )


def _snapshot_request(value: object) -> MicrophoneCaptureRequest:
    if type(value) is not MicrophoneCaptureRequest:
        raise TypeError("request must be an exact MicrophoneCaptureRequest")
    try:
        return MicrophoneCaptureRequest(
            request_id=value.request_id,
            provider_id=value.provider_id,
            device_id=value.device_id,
            sample_rate_hz=value.sample_rate_hz,
            sample_count=value.sample_count,
            policy=_snapshot_policy(value.policy),
        )
    except AttributeError:
        raise TypeError("microphone capture request is incomplete") from None


def _normalized_adapter_error(
    error: MicrophoneCaptureAdapterError,
) -> tuple[MicrophoneCaptureFailureCode, bool]:
    if type(error) is not MicrophoneCaptureAdapterError:
        return MicrophoneCaptureFailureCode.ADAPTER_ERROR, False
    try:
        code = error.code
        retryable = error.retryable
    except AttributeError:
        return MicrophoneCaptureFailureCode.ADAPTER_ERROR, False
    if type(code) is not MicrophoneCaptureFailureCode or type(retryable) is not bool:
        return MicrophoneCaptureFailureCode.ADAPTER_ERROR, False
    return code, retryable


def _consume_future_result(future: asyncio.Future[object]) -> None:
    try:
        future.result()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - late adapter result is intentionally discarded
        return


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
    if type(value) not in (int, float):
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
    if type(value) not in (int, float):
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
