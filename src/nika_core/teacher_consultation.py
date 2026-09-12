from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import (
    ModelGateway,
    model_identity_fingerprint,
)


_MAX_ID_UTF8_BYTES = 256
_MAX_TIMEOUT_SECONDS = 3600.0


class TeacherConsultationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TeacherBudgetStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    WITHIN = "within"
    EXCEEDED = "exceeded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TeacherConsultationPolicy:
    max_request_chars: int = 32_000
    max_response_chars: int = 64_000
    timeout_seconds: float = 60.0
    max_observed_total_tokens: int | None = None

    def __post_init__(self) -> None:
        _positive_int(self.max_request_chars, "max_request_chars")
        _positive_int(self.max_response_chars, "max_response_chars")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a finite number")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be in the range (0, {_MAX_TIMEOUT_SECONDS:g}]"
            )
        if self.max_observed_total_tokens is not None:
            _positive_int(
                self.max_observed_total_tokens, "max_observed_total_tokens"
            )


@dataclass(frozen=True, slots=True)
class TeacherConsultationSpec:
    consultation_id: str
    provider_id: str
    provider_kind: ProviderKind
    model: str
    messages: tuple[ModelMessage, ...]
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    temperature: float | None = None
    policy: TeacherConsultationPolicy = field(
        default_factory=TeacherConsultationPolicy
    )

    def __post_init__(self) -> None:
        _bounded_identity(self.consultation_id, "consultation_id")
        _bounded_identity(self.provider_id, "provider_id")
        _bounded_identity(self.model, "model")
        if self.provider_kind not in {ProviderKind.LOCAL, ProviderKind.CLOUD}:
            raise ValueError("teacher provider_kind must be local or cloud")
        if not isinstance(self.privacy, PrivacyClass):
            raise TypeError("privacy must be a PrivacyClass")
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ValueError("messages must be a non-empty tuple")
        if any(not isinstance(message, ModelMessage) for message in self.messages):
            raise TypeError("messages must contain ModelMessage values")
        request_chars = sum(len(message.content) for message in self.messages)
        if request_chars > self.policy.max_request_chars:
            raise ValueError("teacher consultation request exceeds max_request_chars")


@dataclass(frozen=True, slots=True)
class TeacherConsultationEvidence:
    consultation_id: str
    provider_id: str
    provider_kind: ProviderKind
    requested_model_fingerprint: str
    request_fingerprint: str
    status: TeacherConsultationStatus
    privacy: PrivacyClass
    request_chars: int
    response_chars: int | None
    response_sha256: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: float | None
    budget_status: TeacherBudgetStatus
    error_code: ModelErrorCode | None = None
    retryable: bool | None = None
    failure_effect: ModelFailureEffect | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.teacher-consultation-evidence:v1",
            "consultation_id": self.consultation_id,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind.value,
            "requested_model_fingerprint": self.requested_model_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status.value,
            "privacy": self.privacy.value,
            "request_chars": self.request_chars,
            "response_chars": self.response_chars,
            "response_sha256": self.response_sha256,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "budget_status": self.budget_status.value,
            "error_code": self.error_code.value if self.error_code else None,
            "retryable": self.retryable,
            "failure_effect": (
                self.failure_effect.value if self.failure_effect else None
            ),
        }


@dataclass(frozen=True, slots=True)
class TeacherConsultationResult:
    text: str | None
    evidence: TeacherConsultationEvidence


class TeacherConsultationService:
    """One bounded teacher call over the canonical ModelGateway.

    Response text is transient cognition input. ``TeacherConsultationEvidence``
    is the content-free record intended for durable accounting/reporting.
    """

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def consult(
        self, spec: TeacherConsultationSpec
    ) -> TeacherConsultationResult:
        request_chars = sum(len(message.content) for message in spec.messages)
        request_fingerprint = _fingerprint_messages(spec.messages)
        request = ModelRequest(
            request_id=spec.consultation_id,
            messages=spec.messages,
            model=spec.model,
            provider_id=spec.provider_id,
            provider_kind=spec.provider_kind,
            fallback_provider_ids=(),
            privacy=spec.privacy,
            timeout_seconds=float(spec.policy.timeout_seconds),
            temperature=spec.temperature,
            metadata={"nika.teacher_consultation": "v1"},
        )

        try:
            response = await self._gateway.complete(request)
        except ModelGatewayError as error:
            status = (
                TeacherConsultationStatus.CANCELLED
                if error.code is ModelErrorCode.CANCELLED
                else TeacherConsultationStatus.FAILED
            )
            return TeacherConsultationResult(
                text=None,
                evidence=_failure_evidence(
                    spec=spec,
                    request_chars=request_chars,
                    request_fingerprint=request_fingerprint,
                    status=status,
                    code=error.code,
                    retryable=error.retryable,
                    failure_effect=error.failure_effect,
                ),
            )

        if not _response_identity_matches(spec, response):
            return TeacherConsultationResult(
                text=None,
                evidence=_failure_evidence(
                    spec=spec,
                    request_chars=request_chars,
                    request_fingerprint=request_fingerprint,
                    status=TeacherConsultationStatus.FAILED,
                    code=ModelErrorCode.PROVIDER_ERROR,
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                ),
            )

        try:
            usage = _validated_usage(response)
            latency_ms = _validated_latency(response.latency_ms)
        except (TypeError, ValueError):
            return TeacherConsultationResult(
                text=None,
                evidence=_failure_evidence(
                    spec=spec,
                    request_chars=request_chars,
                    request_fingerprint=request_fingerprint,
                    status=TeacherConsultationStatus.FAILED,
                    code=ModelErrorCode.PROVIDER_ERROR,
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                ),
            )

        if not isinstance(response.text, str) or not response.text:
            return TeacherConsultationResult(
                text=None,
                evidence=_failure_evidence(
                    spec=spec,
                    request_chars=request_chars,
                    request_fingerprint=request_fingerprint,
                    status=TeacherConsultationStatus.FAILED,
                    code=ModelErrorCode.PROVIDER_ERROR,
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                ),
            )

        response_chars = len(response.text)
        if response_chars > spec.policy.max_response_chars:
            return TeacherConsultationResult(
                text=None,
                evidence=_failure_evidence(
                    spec=spec,
                    request_chars=request_chars,
                    request_fingerprint=request_fingerprint,
                    status=TeacherConsultationStatus.FAILED,
                    code=ModelErrorCode.RESOURCE_LIMIT,
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                ),
            )

        budget_status = _budget_status(
            limit=spec.policy.max_observed_total_tokens,
            total_tokens=usage[2],
        )
        response_sha256 = _sha256_text(response.text)
        evidence = TeacherConsultationEvidence(
            consultation_id=spec.consultation_id,
            provider_id=spec.provider_id,
            provider_kind=spec.provider_kind,
            requested_model_fingerprint=model_identity_fingerprint(spec.model),
            request_fingerprint=request_fingerprint,
            status=TeacherConsultationStatus.SUCCEEDED,
            privacy=spec.privacy,
            request_chars=request_chars,
            response_chars=response_chars,
            response_sha256=response_sha256,
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=usage[2],
            latency_ms=latency_ms,
            budget_status=budget_status,
        )
        return TeacherConsultationResult(text=response.text, evidence=evidence)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_identity(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid UTF-8 text") from error
    if len(encoded) > _MAX_ID_UTF8_BYTES:
        raise ValueError(f"{name} exceeds {_MAX_ID_UTF8_BYTES} UTF-8 bytes")
    return value


def _fingerprint_messages(messages: tuple[ModelMessage, ...]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        for value in (message.role, message.content):
            encoded = value.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _sha256_text(value: str) -> str:
    digest = hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"sha256:{digest}"


def _validated_usage(response: ModelResponse) -> tuple[int | None, int | None, int | None]:
    values = (
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.usage.total_tokens,
    )
    for value in values:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("model usage counters must be non-negative integers or None")
    return values


def _validated_latency(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("latency_ms must be a finite non-negative number or None")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("latency_ms must be a finite non-negative number or None")
    return normalized


def _response_identity_matches(
    spec: TeacherConsultationSpec, response: ModelResponse
) -> bool:
    return (
        isinstance(response, ModelResponse)
        and response.request_id == spec.consultation_id
        and response.provider_id == spec.provider_id
        and response.provider_kind is spec.provider_kind
        and response.model == spec.model
    )


def _budget_status(
    *, limit: int | None, total_tokens: int | None
) -> TeacherBudgetStatus:
    if limit is None:
        return TeacherBudgetStatus.NOT_CONFIGURED
    if total_tokens is None:
        return TeacherBudgetStatus.UNKNOWN
    if total_tokens > limit:
        return TeacherBudgetStatus.EXCEEDED
    return TeacherBudgetStatus.WITHIN


def _failure_evidence(
    *,
    spec: TeacherConsultationSpec,
    request_chars: int,
    request_fingerprint: str,
    status: TeacherConsultationStatus,
    code: ModelErrorCode,
    retryable: bool,
    failure_effect: ModelFailureEffect,
) -> TeacherConsultationEvidence:
    return TeacherConsultationEvidence(
        consultation_id=spec.consultation_id,
        provider_id=spec.provider_id,
        provider_kind=spec.provider_kind,
        requested_model_fingerprint=model_identity_fingerprint(spec.model),
        request_fingerprint=request_fingerprint,
        status=status,
        privacy=spec.privacy,
        request_chars=request_chars,
        response_chars=None,
        response_sha256=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        latency_ms=None,
        budget_status=_budget_status(
            limit=spec.policy.max_observed_total_tokens,
            total_tokens=None,
        ),
        error_code=code,
        retryable=retryable,
        failure_effect=failure_effect,
    )
