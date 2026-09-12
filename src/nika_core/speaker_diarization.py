from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 48_000
MIN_AUDIO_SECONDS = 0.25
MAX_AUDIO_SECONDS = 600.0
MAX_ID_CHARS = 128
MAX_SPEAKERS = 32
MAX_SEGMENTS = 20_000
MAX_AUDIO_BYTES = 64 * 1024 * 1024
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class DiarizationErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    RESOURCE_LIMIT = "resource_limit"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ADAPTER_FAILURE = "adapter_failure"
    ADAPTER_TIMEOUT = "adapter_timeout"
    ROUTE_MISMATCH = "route_mismatch"
    INVALID_RESPONSE = "invalid_response"


class DiarizerKind(StrEnum):
    LOCAL = "local"


class DiarizationError(RuntimeError):
    def __init__(
        self,
        code: DiarizationErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DiarizerCapabilities:
    provider_id: str
    model_id: str
    supports_overlap: bool
    max_speakers: int
    kind: DiarizerKind = DiarizerKind.LOCAL

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, field="provider_id")
        _safe_id(self.model_id, field="model_id")
        if type(self.supports_overlap) is not bool:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "supports_overlap must be bool",
            )
        if (
            type(self.max_speakers) is not int
            or not 1 <= self.max_speakers <= MAX_SPEAKERS
        ):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                f"max_speakers must be 1..{MAX_SPEAKERS}",
            )
        if (
            not isinstance(self.kind, DiarizerKind)
            or self.kind is not DiarizerKind.LOCAL
        ):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "speaker diarization boundary supports local adapters only",
            )


@dataclass(frozen=True, slots=True)
class DiarizationPolicy:
    max_audio_bytes: int = 32 * 1024 * 1024
    max_segments: int = 4_096
    max_speakers: int = 16
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        _bounded_positive_int(
            self.max_audio_bytes,
            field="max_audio_bytes",
            maximum=MAX_AUDIO_BYTES,
        )
        _bounded_positive_int(
            self.max_segments,
            field="max_segments",
            maximum=MAX_SEGMENTS,
        )
        _bounded_positive_int(
            self.max_speakers,
            field="max_speakers",
            maximum=MAX_SPEAKERS,
        )
        _finite_number(
            self.timeout_seconds,
            field="timeout_seconds",
            minimum_exclusive=0.0,
            maximum=3_600.0,
        )


@dataclass(frozen=True, slots=True)
class DiarizationRequest:
    request_id: str
    pcm_s16le: bytes
    sample_rate_hz: int = 16_000

    def __post_init__(self) -> None:
        _safe_id(self.request_id, field="request_id")
        if type(self.pcm_s16le) is not bytes or not self.pcm_s16le:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "diarization audio must be non-empty PCM bytes",
            )
        if len(self.pcm_s16le) % 2:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "diarization audio must contain complete signed 16-bit samples",
            )
        if len(self.pcm_s16le) > MAX_AUDIO_BYTES:
            raise DiarizationError(
                DiarizationErrorCode.RESOURCE_LIMIT,
                f"diarization audio must not exceed {MAX_AUDIO_BYTES} bytes",
            )
        if (
            type(self.sample_rate_hz) is not int
            or self.sample_rate_hz < MIN_SAMPLE_RATE_HZ
            or self.sample_rate_hz > MAX_SAMPLE_RATE_HZ
        ):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                f"sample_rate_hz must be {MIN_SAMPLE_RATE_HZ}..{MAX_SAMPLE_RATE_HZ}",
            )
        duration = self.duration_seconds
        if duration < MIN_AUDIO_SECONDS or duration > MAX_AUDIO_SECONDS:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                f"audio duration must be {MIN_AUDIO_SECONDS}..{MAX_AUDIO_SECONDS} seconds",
            )

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm_s16le) / (self.sample_rate_hz * 2)

    @property
    def audio_sha256(self) -> str:
        return hashlib.sha256(self.pcm_s16le).hexdigest()


@dataclass(frozen=True, slots=True)
class DiarizerSegment:
    start_ms: int
    end_ms: int
    speaker_label: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class DiarizerResponse:
    request_id: str
    provider_id: str
    model_id: str
    source_audio_sha256: str
    segments: tuple[DiarizerSegment, ...]
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class SpeakerSegment:
    start_ms: int
    end_ms: int
    speaker_index: int
    confidence: float | None


@dataclass(frozen=True, slots=True)
class DiarizationEvidence:
    request_id: str
    provider_id: str
    model_id: str
    audio_sha256: str
    audio_byte_count: int
    sample_rate_hz: int
    duration_ms: int
    segment_count: int
    speaker_count: int
    overlap_detected: bool
    timeline_sha256: str
    latency_ms: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.speaker-diarization-evidence:v1",
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "audio_sha256": self.audio_sha256,
            "audio_byte_count": self.audio_byte_count,
            "sample_rate_hz": self.sample_rate_hz,
            "duration_ms": self.duration_ms,
            "segment_count": self.segment_count,
            "speaker_count": self.speaker_count,
            "overlap_detected": self.overlap_detected,
            "timeline_sha256": self.timeline_sha256,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class DiarizationResult:
    segments: tuple[SpeakerSegment, ...]
    evidence: DiarizationEvidence


class SpeakerDiarizerAdapter(Protocol):
    @property
    def capabilities(self) -> DiarizerCapabilities: ...

    async def diarize(self, request: DiarizationRequest) -> DiarizerResponse: ...


class UnavailableSpeakerDiarizerAdapter:
    def __init__(self, *, provider_id: str = "speaker-diarizer-unavailable") -> None:
        self._capabilities = DiarizerCapabilities(
            provider_id=provider_id,
            model_id="unavailable",
            supports_overlap=False,
            max_speakers=1,
        )

    @property
    def capabilities(self) -> DiarizerCapabilities:
        return self._capabilities

    async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
        del request
        raise DiarizationError(
            DiarizationErrorCode.ADAPTER_UNAVAILABLE,
            "local speaker diarization is not configured",
            retryable=True,
        )


class SpeakerDiarizationService:
    """Local provider-neutral diarization boundary with content-free evidence."""

    def __init__(
        self,
        adapter: SpeakerDiarizerAdapter,
        *,
        policy: DiarizationPolicy | None = None,
    ) -> None:
        capabilities = _read_capabilities(
            adapter,
            error_code=DiarizationErrorCode.INVALID_REQUEST,
        )
        if policy is not None and not isinstance(policy, DiarizationPolicy):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "diarization policy must use the canonical type",
            )
        self._adapter = adapter
        self._capabilities = capabilities
        self._policy = policy if policy is not None else DiarizationPolicy()

    @property
    def capabilities(self) -> DiarizerCapabilities:
        return self._capabilities

    async def diarize(self, request: DiarizationRequest) -> DiarizationResult:
        if not isinstance(request, DiarizationRequest):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_REQUEST,
                "request must be a DiarizationRequest",
            )
        if len(request.pcm_s16le) > self._policy.max_audio_bytes:
            raise DiarizationError(
                DiarizationErrorCode.RESOURCE_LIMIT,
                "audio exceeds the configured diarization byte budget",
            )
        self._assert_route_unchanged("before diarization")

        try:
            response = await asyncio.wait_for(
                self._adapter.diarize(request),
                timeout=_timeout_value(self._policy.timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise DiarizationError(
                DiarizationErrorCode.ADAPTER_TIMEOUT,
                "speaker diarization exceeded its deadline",
                retryable=True,
            ) from None
        except DiarizationError as error:
            raise _normalized_adapter_error(error) from None
        except Exception:  # noqa: BLE001 - provider diagnostics stay behind this boundary
            raise DiarizationError(
                DiarizationErrorCode.ADAPTER_FAILURE,
                "speaker diarization adapter failed",
            ) from None

        self._assert_route_unchanged("during diarization")
        segments, overlap_detected, latency_ms = self._validate_response(
            response,
            request,
        )
        timeline_sha256 = _timeline_sha256(segments)
        return DiarizationResult(
            segments=segments,
            evidence=DiarizationEvidence(
                request_id=request.request_id,
                provider_id=self._capabilities.provider_id,
                model_id=self._capabilities.model_id,
                audio_sha256=request.audio_sha256,
                audio_byte_count=len(request.pcm_s16le),
                sample_rate_hz=request.sample_rate_hz,
                duration_ms=_duration_ms(request),
                segment_count=len(segments),
                speaker_count=len({segment.speaker_index for segment in segments}),
                overlap_detected=overlap_detected,
                timeline_sha256=timeline_sha256,
                latency_ms=latency_ms,
            ),
        )

    def _assert_route_unchanged(self, phase: str) -> None:
        current = _read_capabilities(
            self._adapter,
            error_code=DiarizationErrorCode.ROUTE_MISMATCH,
        )
        if current != self._capabilities:
            raise DiarizationError(
                DiarizationErrorCode.ROUTE_MISMATCH,
                f"speaker diarizer route changed {phase}",
            )

    def _validate_response(
        self,
        response: object,
        request: DiarizationRequest,
    ) -> tuple[tuple[SpeakerSegment, ...], bool, float | None]:
        if not isinstance(response, DiarizerResponse):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_RESPONSE,
                "speaker diarizer returned an invalid response type",
            )
        if (
            response.request_id != request.request_id
            or response.provider_id != self._capabilities.provider_id
            or response.model_id != self._capabilities.model_id
        ):
            raise DiarizationError(
                DiarizationErrorCode.ROUTE_MISMATCH,
                "speaker diarizer response identity does not match configured route",
            )
        digest = _sha256(
            response.source_audio_sha256,
            field="source_audio_sha256",
        )
        if digest != request.audio_sha256:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_RESPONSE,
                "speaker diarizer response is bound to different audio",
            )
        if type(response.segments) is not tuple:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_RESPONSE,
                "speaker diarizer segments must be an immutable tuple",
            )
        if len(response.segments) > self._policy.max_segments:
            raise DiarizationError(
                DiarizationErrorCode.RESOURCE_LIMIT,
                "speaker diarizer returned too many segments",
            )

        duration_ms = _duration_ms(request)
        speaker_limit = min(
            self._policy.max_speakers,
            self._capabilities.max_speakers,
        )
        raw_labels: dict[str, int] = {}
        normalized: list[SpeakerSegment] = []
        previous_start = -1
        running_max_end = -1
        overlap_detected = False

        for raw in response.segments:
            if not isinstance(raw, DiarizerSegment):
                raise DiarizationError(
                    DiarizationErrorCode.INVALID_RESPONSE,
                    "speaker diarizer returned an invalid segment type",
                )
            _validate_segment(raw, duration_ms=duration_ms)
            if raw.start_ms < previous_start:
                raise DiarizationError(
                    DiarizationErrorCode.INVALID_RESPONSE,
                    "speaker diarizer segments must use chronological ordering",
                )
            if running_max_end >= 0 and raw.start_ms < running_max_end:
                overlap_detected = True
            previous_start = raw.start_ms
            running_max_end = max(running_max_end, raw.end_ms)

            if raw.speaker_label not in raw_labels:
                if len(raw_labels) >= speaker_limit:
                    raise DiarizationError(
                        DiarizationErrorCode.RESOURCE_LIMIT,
                        "speaker diarizer returned too many distinct speakers",
                    )
                raw_labels[raw.speaker_label] = len(raw_labels) + 1
            normalized.append(
                SpeakerSegment(
                    start_ms=raw.start_ms,
                    end_ms=raw.end_ms,
                    speaker_index=raw_labels[raw.speaker_label],
                    confidence=_optional_confidence(raw.confidence),
                )
            )

        if overlap_detected and not self._capabilities.supports_overlap:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_RESPONSE,
                "speaker diarizer returned overlap without declaring overlap support",
            )
        latency_ms = _optional_non_negative_number(
            response.latency_ms,
            field="latency_ms",
        )
        return tuple(normalized), overlap_detected, latency_ms


def _read_capabilities(
    adapter: object,
    *,
    error_code: DiarizationErrorCode,
) -> DiarizerCapabilities:
    try:
        value = adapter.capabilities  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - adapter diagnostics stay private
        raise DiarizationError(
            error_code,
            "speaker diarizer capabilities are unavailable or invalid",
        ) from None
    return _validated_capabilities(value, error_code=error_code)


def _validated_capabilities(
    value: object,
    *,
    error_code: DiarizationErrorCode,
) -> DiarizerCapabilities:
    if not isinstance(value, DiarizerCapabilities):
        raise DiarizationError(
            error_code,
            "speaker diarizer capabilities must use the canonical type",
        )
    _safe_id(value.provider_id, field="provider_id", error_code=error_code)
    _safe_id(value.model_id, field="model_id", error_code=error_code)
    if type(value.supports_overlap) is not bool:
        raise DiarizationError(
            error_code,
            "speaker diarizer overlap capability must be bool",
        )
    if (
        type(value.max_speakers) is not int
        or not 1 <= value.max_speakers <= MAX_SPEAKERS
    ):
        raise DiarizationError(
            error_code,
            "speaker diarizer max_speakers capability is invalid",
        )
    if (
        not isinstance(value.kind, DiarizerKind)
        or value.kind is not DiarizerKind.LOCAL
    ):
        raise DiarizationError(
            error_code,
            "speaker diarizer must declare local execution",
        )
    return value


def _normalized_adapter_error(error: DiarizationError) -> DiarizationError:
    if error.code is DiarizationErrorCode.ADAPTER_UNAVAILABLE:
        return DiarizationError(
            DiarizationErrorCode.ADAPTER_UNAVAILABLE,
            "local speaker diarization is unavailable",
            retryable=error.retryable,
        )
    if error.code is DiarizationErrorCode.ADAPTER_TIMEOUT:
        return DiarizationError(
            DiarizationErrorCode.ADAPTER_TIMEOUT,
            "speaker diarization adapter timed out",
            retryable=True,
        )
    return DiarizationError(
        DiarizationErrorCode.ADAPTER_FAILURE,
        "speaker diarization adapter failed",
    )


def _validate_segment(segment: DiarizerSegment, *, duration_ms: int) -> None:
    if type(segment.start_ms) is not int or type(segment.end_ms) is not int:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment timestamps must be integer milliseconds",
        )
    if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment timestamps must define a positive interval",
        )
    if segment.end_ms > duration_ms:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment exceeds source audio duration",
        )
    _safe_id(
        segment.speaker_label,
        field="speaker_label",
        error_code=DiarizationErrorCode.INVALID_RESPONSE,
    )
    _optional_confidence(segment.confidence)


def _safe_id(
    value: object,
    *,
    field: str,
    error_code: DiarizationErrorCode = DiarizationErrorCode.INVALID_REQUEST,
) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_ID_CHARS
        or not _SAFE_ID_RE.fullmatch(value)
    ):
        raise DiarizationError(
            error_code,
            f"{field} must be a bounded safe identifier",
        )
    return value


def _sha256(value: object, *, field: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _bounded_positive_int(value: object, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must be an integer in 1..{maximum}",
        )
    return value


def _finite_number(
    value: object,
    *,
    field: str,
    minimum_exclusive: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must be a finite number",
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must be a finite number",
        ) from None
    if not math.isfinite(normalized):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must be a finite number",
        )
    if minimum_exclusive is not None and normalized <= minimum_exclusive:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must be greater than {minimum_exclusive}",
        )
    if maximum is not None and normalized > maximum:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_REQUEST,
            f"{field} must not exceed {maximum}",
        )
    return normalized


def _timeout_value(value: object) -> float:
    return _finite_number(
        value,
        field="timeout_seconds",
        minimum_exclusive=0.0,
        maximum=3_600.0,
    )


def _optional_confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment confidence must be a finite number in [0, 1] or None",
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment confidence must be a finite number in [0, 1] or None",
        ) from None
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            "segment confidence must be a finite number in [0, 1] or None",
        )
    return normalized


def _optional_non_negative_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            f"{field} must be a finite non-negative number or None",
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            f"{field} must be a finite non-negative number or None",
        ) from None
    if not math.isfinite(normalized) or normalized < 0:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_RESPONSE,
            f"{field} must be a finite non-negative number or None",
        )
    return normalized


def _duration_ms(request: DiarizationRequest) -> int:
    sample_count = len(request.pcm_s16le) // 2
    return math.ceil(sample_count * 1_000 / request.sample_rate_hz)


def _timeline_sha256(segments: tuple[SpeakerSegment, ...]) -> str:
    payload = [
        {
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker_index": segment.speaker_index,
            "confidence": segment.confidence,
        }
        for segment in segments
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "DiarizationError",
    "DiarizationErrorCode",
    "DiarizationEvidence",
    "DiarizationPolicy",
    "DiarizationRequest",
    "DiarizationResult",
    "DiarizerCapabilities",
    "DiarizerKind",
    "DiarizerResponse",
    "DiarizerSegment",
    "SpeakerDiarizationService",
    "SpeakerDiarizerAdapter",
    "SpeakerSegment",
    "UnavailableSpeakerDiarizerAdapter",
]
