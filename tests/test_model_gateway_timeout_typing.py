from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import DeterministicMockProvider


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "timeout-typing",
        "messages": (ModelMessage(role="user", content="hello"),),
        "provider_id": "primary",
        "provider_kind": None,
        "privacy": PrivacyClass.PRIVATE,
        "timeout_seconds": 1.0,
        "fallback_provider_ids": ("fallback",),
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


class _HardCancellableProvider(DeterministicMockProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self._capabilities.provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=True,
        )


class _RecordingFallback(DeterministicMockProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="fallback")
        self.called = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.called = True
        return await super().complete(request)


def test_untyped_provider_timeout_never_falls_through_even_with_hard_cancel_claim() -> None:
    class RawTimeoutProvider(_HardCancellableProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise TimeoutError("provider-specific timeout without Nika typing")

    primary = RawTimeoutProvider(provider_id="primary")
    fallback = _RecordingFallback()
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.provider_id == "primary"
    assert exc_info.value.retryable is False
    assert fallback.called is False


def test_typed_retryable_timeout_can_fallback_only_with_hard_cancellation_evidence() -> None:
    class TypedTimeoutProvider(_HardCancellableProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "typed provider timeout",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    primary = TypedTimeoutProvider(provider_id="primary")
    fallback = _RecordingFallback()
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    response = asyncio.run(gateway.complete(_request()))

    assert response.provider_id == "fallback"
    assert fallback.called is True


@pytest.mark.parametrize("timeout_seconds", [True, "1"])
def test_request_rejects_non_numeric_or_boolean_deadline(timeout_seconds: object) -> None:
    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        _request(timeout_seconds=timeout_seconds)


@pytest.mark.parametrize(
    "timeout_seconds",
    [float("inf"), float("-inf"), float("nan")],
)
def test_request_rejects_non_finite_deadline(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite"):
        _request(timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("temperature", [True, "0.5"])
def test_request_rejects_non_numeric_or_boolean_temperature(temperature: object) -> None:
    with pytest.raises(TypeError, match="temperature must be numeric"):
        _request(temperature=temperature)


@pytest.mark.parametrize(
    "temperature",
    [float("inf"), float("-inf"), float("nan")],
)
def test_request_rejects_non_finite_temperature(temperature: float) -> None:
    with pytest.raises(ValueError, match="temperature must be finite"):
        _request(temperature=temperature)


def test_request_accepts_finite_numeric_deadline_and_temperature() -> None:
    request = _request(timeout_seconds=2, temperature=0)

    assert request.timeout_seconds == 2
    assert request.temperature == 0
