from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.route import RoutedModelProvider


class _ForeignErrorProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="expected-upstream",
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelGatewayError(
            ModelErrorCode.RATE_LIMITED,
            "SECRET-FOREIGN-DIAGNOSTIC",
            provider_id="foreign-upstream",
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


class _MalformedCapabilitiesProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="upstream",
            kind="cloud",  # type: ignore[arg-type]
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"provider must not be called: {request.request_id}")


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="route-authority",
        messages=(ModelMessage(role="user", content="route authority proof"),),
        provider_id="route-a",
        model="model-a",
    )


def test_foreign_upstream_error_identity_loses_retry_no_effect_authority() -> None:
    async def scenario() -> ModelGatewayError:
        gateway = ModelGateway()
        gateway.register(
            RoutedModelProvider(route_id="route-a", provider=_ForeignErrorProvider())
        )
        with pytest.raises(ModelGatewayError) as captured:
            await gateway.complete(_request())
        return captured.value

    error = asyncio.run(scenario())

    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "route-a"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert "SECRET-FOREIGN-DIAGNOSTIC" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_malformed_capability_kind_fails_before_route_registration_or_effect() -> None:
    with pytest.raises(TypeError, match="kind must be ProviderKind"):
        RoutedModelProvider(
            route_id="route-a",
            provider=_MalformedCapabilitiesProvider(),
        )
