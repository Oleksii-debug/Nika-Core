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
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.route import RoutedModelProvider


class _DriftingTypedFailureProvider:
    def __init__(self) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="upstream-engine",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        self._capabilities = ProviderCapabilities(
            provider_id="changed-after-effect",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        raise ModelGatewayError(
            ModelErrorCode.RATE_LIMITED,
            "SECRET-DRIFT-DIAGNOSTIC",
            provider_id="upstream-engine",
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


class _FallbackProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="fallback",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="fallback must not execute",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


def test_typed_safe_failure_loses_fallback_authority_after_capability_drift() -> None:
    async def scenario() -> tuple[ModelGatewayError, int, int]:
        primary = _DriftingTypedFailureProvider()
        fallback = _FallbackProvider()
        gateway = ModelGateway()
        gateway.register(RoutedModelProvider(route_id="route-primary", provider=primary))
        gateway.register(fallback)
        request = ModelRequest(
            request_id="drift-failure",
            messages=(ModelMessage(role="user", content="route drift proof"),),
            provider_id="route-primary",
            model="m",
            fallback_provider_ids=("fallback",),
            privacy=PrivacyClass.PUBLIC,
            timeout_seconds=2.0,
        )
        with pytest.raises(ModelGatewayError) as caught:
            await gateway.complete(request)
        return caught.value, primary.calls, fallback.calls

    error, primary_calls, fallback_calls = asyncio.run(scenario())

    assert primary_calls == 1
    assert fallback_calls == 0
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "route-primary"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert "SECRET-DRIFT-DIAGNOSTIC" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
