from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.teacher_consultation import (
    TeacherBudgetStatus,
    TeacherConsultationPolicy,
    TeacherConsultationService,
    TeacherConsultationSpec,
    TeacherConsultationStatus,
)


class _FakeProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        text: str = "teacher answer",
        model: str = "teacher-model",
        total_tokens: int | None = 7,
        supports_private_data: bool = True,
        error: ModelGatewayError | None = None,
        response_request_id: str | None = None,
        response_provider_id: str | None = None,
        response_kind: ProviderKind | None = None,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
        )
        self.text = text
        self.model = model
        self.total_tokens = total_tokens
        self.error = error
        self.response_request_id = response_request_id
        self.response_provider_id = response_provider_id
        self.response_kind = response_kind
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            request_id=self.response_request_id or request.request_id,
            text=self.text,
            provider_id=self.response_provider_id or self.capabilities.provider_id,
            provider_kind=self.response_kind or self.capabilities.kind,
            model=self.model,
            usage=ModelUsage(
                input_tokens=3,
                output_tokens=4,
                total_tokens=self.total_tokens,
            ),
            latency_ms=12.5,
        )


def _spec(
    *,
    provider_id: str = "teacher-local",
    kind: ProviderKind = ProviderKind.LOCAL,
    model: str = "teacher-model",
    policy: TeacherConsultationPolicy | None = None,
    privacy: PrivacyClass = PrivacyClass.PRIVATE,
) -> TeacherConsultationSpec:
    return TeacherConsultationSpec(
        consultation_id="consultation-001",
        provider_id=provider_id,
        provider_kind=kind,
        model=model,
        messages=(
            ModelMessage(role="system", content="Act as a bounded teacher."),
            ModelMessage(role="user", content="Explain the evidence."),
        ),
        privacy=privacy,
        policy=policy or TeacherConsultationPolicy(),
    )


def test_local_teacher_call_is_explicit_bounded_and_content_free_in_evidence() -> None:
    provider = _FakeProvider(provider_id="teacher-local", kind=ProviderKind.LOCAL)
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)

    result = asyncio.run(service.consult(_spec()))

    assert result.text == "teacher answer"
    assert result.evidence.status is TeacherConsultationStatus.SUCCEEDED
    assert result.evidence.provider_id == "teacher-local"
    assert result.evidence.provider_kind is ProviderKind.LOCAL
    assert result.evidence.total_tokens == 7
    assert result.evidence.budget_status is TeacherBudgetStatus.NOT_CONFIGURED
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.provider_id == "teacher-local"
    assert request.provider_kind is None
    assert request.model == "teacher-model"
    assert request.fallback_provider_ids == ()
    assert request.timeout_seconds <= 60.0

    durable = json.dumps(result.evidence.as_dict(), sort_keys=True)
    assert "Act as a bounded teacher." not in durable
    assert "Explain the evidence." not in durable
    assert "teacher answer" not in durable
    assert result.evidence.request_fingerprint.startswith("sha256:")
    assert result.evidence.response_sha256 is not None


def test_cloud_teacher_uses_selected_provider_without_fallback() -> None:
    error = ModelGatewayError(
        ModelErrorCode.RATE_LIMITED,
        "provider-controlled secret detail",
        provider_id="teacher-cloud",
        retryable=True,
        failure_effect=ModelFailureEffect.NO_EFFECT,
    )
    primary = _FakeProvider(
        provider_id="teacher-cloud",
        kind=ProviderKind.CLOUD,
        error=error,
    )
    fallback = _FakeProvider(
        provider_id="other-cloud",
        kind=ProviderKind.CLOUD,
        text="must not run",
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)
    service = TeacherConsultationService(gateway)

    result = asyncio.run(
        service.consult(
            _spec(provider_id="teacher-cloud", kind=ProviderKind.CLOUD)
        )
    )

    assert result.text is None
    assert result.evidence.status is TeacherConsultationStatus.FAILED
    assert result.evidence.error_code is ModelErrorCode.RATE_LIMITED
    assert result.evidence.failure_effect is ModelFailureEffect.NO_EFFECT
    assert len(primary.requests) == 1
    assert fallback.requests == []


def test_private_consultation_fails_before_provider_without_private_data_support() -> None:
    provider = _FakeProvider(
        provider_id="public-only",
        kind=ProviderKind.CLOUD,
        supports_private_data=False,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)

    result = asyncio.run(
        service.consult(
            _spec(provider_id="public-only", kind=ProviderKind.CLOUD)
        )
    )

    assert result.text is None
    assert result.evidence.status is TeacherConsultationStatus.FAILED
    assert result.evidence.error_code is ModelErrorCode.INVALID_REQUEST
    assert provider.requests == []


def test_request_bound_fails_before_gateway_call() -> None:
    with pytest.raises(ValueError, match="max_request_chars"):
        _spec(
            policy=TeacherConsultationPolicy(
                max_request_chars=10,
                max_response_chars=100,
            )
        )


def test_oversized_response_is_not_returned_to_cognition() -> None:
    provider = _FakeProvider(
        provider_id="teacher-local",
        kind=ProviderKind.LOCAL,
        text="123456",
    )
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)
    policy = TeacherConsultationPolicy(
        max_request_chars=100,
        max_response_chars=5,
    )

    result = asyncio.run(service.consult(_spec(policy=policy)))

    assert result.text is None
    assert result.evidence.status is TeacherConsultationStatus.FAILED
    assert result.evidence.error_code is ModelErrorCode.RESOURCE_LIMIT
    assert result.evidence.response_sha256 is None


@pytest.mark.parametrize(
    ("total_tokens", "expected"),
    [
        (7, TeacherBudgetStatus.WITHIN),
        (11, TeacherBudgetStatus.EXCEEDED),
        (None, TeacherBudgetStatus.UNKNOWN),
    ],
)
def test_observed_token_budget_is_truthful_not_inferred(
    total_tokens: int | None,
    expected: TeacherBudgetStatus,
) -> None:
    provider = _FakeProvider(
        provider_id="teacher-local",
        kind=ProviderKind.LOCAL,
        total_tokens=total_tokens,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)
    policy = TeacherConsultationPolicy(
        max_request_chars=100,
        max_response_chars=100,
        max_observed_total_tokens=10,
    )

    result = asyncio.run(service.consult(_spec(policy=policy)))

    assert result.evidence.status is TeacherConsultationStatus.SUCCEEDED
    assert result.evidence.budget_status is expected
    assert result.evidence.total_tokens == total_tokens


def test_mismatched_teacher_response_identity_fails_closed() -> None:
    provider = _FakeProvider(
        provider_id="teacher-local",
        kind=ProviderKind.LOCAL,
        response_request_id="other-request",
    )
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)

    result = asyncio.run(service.consult(_spec()))

    assert result.text is None
    assert result.evidence.status is TeacherConsultationStatus.FAILED
    assert result.evidence.error_code is ModelErrorCode.PROVIDER_ERROR
    assert result.evidence.failure_effect is ModelFailureEffect.UNKNOWN


def test_caller_cancellation_propagates_instead_of_becoming_learning_evidence() -> None:
    class _CancelledProvider(_FakeProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            raise asyncio.CancelledError()

    provider = _CancelledProvider(
        provider_id="teacher-local",
        kind=ProviderKind.LOCAL,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    service = TeacherConsultationService(gateway)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.consult(_spec()))

    assert len(provider.requests) == 1


@pytest.mark.parametrize("timeout", [0.0, float("nan"), float("inf"), 3600.1])
def test_timeout_policy_is_finite_positive_and_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        TeacherConsultationPolicy(timeout_seconds=timeout)
