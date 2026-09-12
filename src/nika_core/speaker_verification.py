from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Protocol

MIN_AUDIO_SECONDS = 0.25
MAX_AUDIO_SECONDS = 30.0
MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 48_000
MAX_ID_CHARS = 128
_MAX_TIMEOUT_SECONDS = 300.0
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class SpeakerVerificationErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ADAPTER_FAILURE = "adapter_failure"
    ADAPTER_TIMEOUT = "adapter_timeout"
    CANCELLED = "cancelled"
    ROUTE_MISMATCH = "route_mismatch"
    INVALID_RESPONSE = "invalid_response"


class SpeakerVerificationOutcome(StrEnum):
    MATCH = "match"
    UNCERTAIN = "uncertain"
    NO_MATCH = "no_match"


class SpeakerVerifierKind(StrEnum):
    LOCAL = "local"


class SpeakerVerificationError(RuntimeError):
    def __init__(
        self,
        code: SpeakerVerificationErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SpeakerVerifierCapabilities:
    provider_id: str
    model_id: str
    kind: SpeakerVerifierKind = SpeakerVerifierKind.LOCAL

    def __post_init__(self) -> None:
        _require_safe_id(self.provider_id, field="provider_id")
        _require_safe_id(self.model_id, field="model_id")
        if not isinstance(self.kind, SpeakerVerifierKind):
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "speaker verifier kind must be a SpeakerVerifierKind",
            )
        if self.kind is not SpeakerVerifierKind.LOCAL:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "only local speaker verification is supported by this boundary",
            )


@dataclass(frozen=True, slots=True)
class SpeakerVerificationPolicy:
    no_match_at_or_below: float = 0.60
    match_at_or_above: float = 0.85

    def __post_init__(self) -> None:
        low = _confidence(self.no_match_at_or_below, field="no_match_at_or_below")
        high = _confidence(self.match_at_or_above, field="match_at_or_above")
        if low >= high:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "speaker verification thresholds must be strictly ordered",
            )


@dataclass(frozen=True, slots=True)
class SpeakerVerificationRequest:
    request_id: str
    profile_id: str
    pcm_s16le: bytes
    sample_rate_hz: int = 16_000

    def __post_init__(self) -> None:
        _require_safe_id(self.request_id, field="request_id")
        _require_safe_id(self.profile_id, field="profile_id")
        if type(self.pcm_s16le) is not bytes or not self.pcm_s16le:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "speaker audio must be non-empty PCM bytes",
            )
        if len(self.pcm_s16le) % 2:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "speaker audio must contain complete signed 16-bit samples",
            )
        if (
            type(self.sample_rate_hz) is not int
            or self.sample_rate_hz < MIN_SAMPLE_RATE_HZ
            or self.sample_rate_hz > MAX_SAMPLE_RATE_HZ
        ):
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                f"sample_rate_hz must be {MIN_SAMPLE_RATE_HZ}..{MAX_SAMPLE_RATE_HZ}",
            )
        duration = self.duration_seconds
        if duration < MIN_AUDIO_SECONDS or duration > MAX_AUDIO_SECONDS:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                f"speaker audio duration must be {MIN_AUDIO_SECONDS}..{MAX_AUDIO_SECONDS} seconds",
            )

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm_s16le) / (self.sample_rate_hz * 2)


@dataclass(frozen=True, slots=True)
class SpeakerVerifierResponse:
    provider_id: str
    model_id: str
    profile_id: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SpeakerVerificationEvidence:
    request_id: str
    provider_id: str
    model_id: str
    profile_fingerprint_sha256: str
    audio_sha256: str
    audio_byte_count: int
    sample_rate_hz: int
    duration_seconds: float
    confidence: float
    outcome: SpeakerVerificationOutcome


class SpeakerVerifierAdapter(Protocol):
    @property
    def capabilities(self) -> SpeakerVerifierCapabilities: ...

    def verify(
        self,
        request: SpeakerVerificationRequest,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> SpeakerVerifierResponse: ...


class UnavailableSpeakerVerifierAdapter:
    def __init__(self, *, provider_id: str = "speaker-verifier-unavailable") -> None:
        self._capabilities = SpeakerVerifierCapabilities(
            provider_id=provider_id,
            model_id="unavailable",
        )

    @property
    def capabilities(self) -> SpeakerVerifierCapabilities:
        return self._capabilities

    def verify(
        self,
        request: SpeakerVerificationRequest,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> SpeakerVerifierResponse:
        del request, timeout_seconds, cancel_event
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.ADAPTER_UNAVAILABLE,
            "local speaker verification is not configured",
            retryable=True,
        )


class SpeakerVerificationService:
    def __init__(
        self,
        adapter: SpeakerVerifierAdapter,
        *,
        policy: SpeakerVerificationPolicy | None = None,
    ) -> None:
        capabilities = _validated_capabilities(adapter.capabilities)
        self._adapter = adapter
        self._bound_capabilities = capabilities
        self._policy = policy or SpeakerVerificationPolicy()

    @property
    def capabilities(self) -> SpeakerVerifierCapabilities:
        return self._bound_capabilities

    def verify(
        self,
        request: SpeakerVerificationRequest,
        *,
        timeout_seconds: float = 15.0,
        cancel_event: Event | None = None,
    ) -> SpeakerVerificationEvidence:
        if not isinstance(request, SpeakerVerificationRequest):
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_REQUEST,
                "request must be a SpeakerVerificationRequest",
            )
        timeout = _validated_timeout(timeout_seconds)
        if cancel_event is not None and cancel_event.is_set():
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.CANCELLED,
                "speaker verification was cancelled",
            )

        current_capabilities = _validated_capabilities(self._adapter.capabilities)
        if current_capabilities != self._bound_capabilities:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.ROUTE_MISMATCH,
                "speaker verifier route changed after service binding",
            )

        try:
            response = self._adapter.verify(
                request,
                timeout_seconds=timeout,
                cancel_event=cancel_event,
            )
        except SpeakerVerificationError:
            raise
        except TimeoutError:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.ADAPTER_TIMEOUT,
                "speaker verification exceeded its deadline",
                retryable=True,
            ) from None
        except Exception:  # noqa: BLE001 - provider boundary minimizes unknown adapter diagnostics
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.ADAPTER_FAILURE,
                "speaker verification adapter failed",
            ) from None

        after_capabilities = _validated_capabilities(self._adapter.capabilities)
        if after_capabilities != self._bound_capabilities:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.ROUTE_MISMATCH,
                "speaker verifier route changed during verification",
            )
        confidence = self._validate_response(response, request)
        outcome = self._classify(confidence)
        return SpeakerVerificationEvidence(
            request_id=request.request_id,
            provider_id=self._bound_capabilities.provider_id,
            model_id=self._bound_capabilities.model_id,
            profile_fingerprint_sha256=_sha256_text(request.profile_id),
            audio_sha256=hashlib.sha256(request.pcm_s16le).hexdigest(),
            audio_byte_count=len(request.pcm_s16le),
            sample_rate_hz=request.sample_rate_hz,
            duration_seconds=request.duration_seconds,
            confidence=confidence,
            outcome=outcome,
        )

    def _validate_response(
        self,
        response: SpeakerVerifierResponse,
        request: SpeakerVerificationRequest,
    ) -> float:
        if not isinstance(response, SpeakerVerifierResponse):
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_RESPONSE,
                "speaker verifier returned an invalid response type",
            )
        if (
            response.provider_id != self._bound_capabilities.provider_id
            or response.model_id != self._bound_capabilities.model_id
        ):
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.ROUTE_MISMATCH,
                "speaker verifier response route does not match configured authority",
            )
        if response.profile_id != request.profile_id:
            raise SpeakerVerificationError(
                SpeakerVerificationErrorCode.INVALID_RESPONSE,
                "speaker verifier response profile does not match the request",
            )
        return _confidence(response.confidence, field="confidence", response=True)

    def _classify(self, confidence: float) -> SpeakerVerificationOutcome:
        if confidence >= self._policy.match_at_or_above:
            return SpeakerVerificationOutcome.MATCH
        if confidence <= self._policy.no_match_at_or_below:
            return SpeakerVerificationOutcome.NO_MATCH
        return SpeakerVerificationOutcome.UNCERTAIN


def _validated_capabilities(value: object) -> SpeakerVerifierCapabilities:
    if not isinstance(value, SpeakerVerifierCapabilities):
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.INVALID_REQUEST,
            "speaker verifier capabilities must use the canonical type",
        )
    _require_safe_id(value.provider_id, field="provider_id")
    _require_safe_id(value.model_id, field="model_id")
    if not isinstance(value.kind, SpeakerVerifierKind) or value.kind is not SpeakerVerifierKind.LOCAL:
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.INVALID_REQUEST,
            "speaker verifier capabilities must declare local execution",
        )
    return value


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.INVALID_REQUEST,
            "speaker verification timeout must be numeric",
        )
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.INVALID_REQUEST,
            f"speaker verification timeout must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS:g}",
        )
    return timeout


def _confidence(value: object, *, field: str, response: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        code = (
            SpeakerVerificationErrorCode.INVALID_RESPONSE
            if response
            else SpeakerVerificationErrorCode.INVALID_REQUEST
        )
        raise SpeakerVerificationError(code, f"{field} must be numeric")
    confidence = float(value)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        code = (
            SpeakerVerificationErrorCode.INVALID_RESPONSE
            if response
            else SpeakerVerificationErrorCode.INVALID_REQUEST
        )
        raise SpeakerVerificationError(code, f"{field} must be finite and within 0..1")
    return confidence


def _require_safe_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_ID_CHARS or not _SAFE_ID_RE.fullmatch(value):
        raise SpeakerVerificationError(
            SpeakerVerificationErrorCode.INVALID_REQUEST,
            f"{field} must be a bounded safe identifier",
        )
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
