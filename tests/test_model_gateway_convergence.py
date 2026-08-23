from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _Provider:
    def __init__(
        self,
        provider_id: str,
        *,
        response_model: str = "model-a",
        error: ModelGatewayError | None = None,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self._response_model = response_model
        self._error = error
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self._capabilities.provider_id,
            provider_kind=self._capabilities.kind,
            model=self._response_model,
            usage=ModelUsage(input_tokens=1, output_tokens=2, total_tokens=3),
        )


def _request(*, provider_id: str = "primary", fallbacks: tuple[str, ...] = ()) -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="private payload"),),
        model="model-a",
        provider_id=provider_id,
        fallback_provider_ids=fallbacks,
    )


def test_usage_total_must_equal_known_components_even_when_components_sum_to_zero() -> None:
    with pytest.raises(ValueError, match="must equal"):
        ModelUsage(input_tokens=0, output_tokens=0, total_tokens=1)
    with pytest.raises(ValueError, match="must equal"):
        ModelUsage(input_tokens=2, output_tokens=3, total_tokens=6)


def test_resource_policy_rejects_boolean_and_non_finite_numeric_values() -> None:
    with pytest.raises(TypeError, match="max_cpu_percent"):
        ModelResourcePolicy(max_cpu_percent=True)
    with pytest.raises(ValueError, match="finite"):
        ModelResourcePolicy(max_memory_percent=float("inf"))
    with pytest.raises(TypeError, match="min_available_memory_bytes"):
        ModelResourcePolicy(min_available_memory_bytes=True)


def test_explicit_requested_model_identity_mismatch_fails_closed() -> None:
    provider = _Provider("primary", response_model="model-b")
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(gateway.complete(_request()))

    assert raised.value.code is ModelErrorCode.PROVIDER_ERROR
    assert raised.value.retryable is False
    assert provider.calls == 1


def test_retryable_generic_provider_error_cannot_trigger_fallback() -> None:
    primary = _Provider(
        "primary",
        error=ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "generic failure",
            provider_id="primary",
            retryable=True,
        ),
    )
    fallback = _Provider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(gateway.complete(_request(fallbacks=("fallback",))))

    assert raised.value.code is ModelErrorCode.PROVIDER_ERROR
    assert primary.calls == 1
    assert fallback.calls == 0


def test_non_boolean_retryable_flag_is_normalized_fail_closed() -> None:
    primary = _Provider(
        "primary",
        error=ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "malformed retry flag",
            provider_id="primary",
            retryable=True,
        ),
    )
    primary._error.retryable = 1  # type: ignore[union-attr]
    fallback = _Provider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(gateway.complete(_request(fallbacks=("fallback",))))

    assert raised.value.code is ModelErrorCode.PROVIDER_ERROR
    assert raised.value.retryable is False
    assert fallback.calls == 0
