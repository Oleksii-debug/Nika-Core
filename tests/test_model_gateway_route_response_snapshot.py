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
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.route import RoutedModelProvider


class _AlwaysEqualText(str):
    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False


class _ResponseSubclass(ModelResponse):
    pass


class _UsageSubclass(ModelUsage):
    pass


class _StaticProvider:
    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="upstream-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        return self._response


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="route response snapshot proof"),),
        provider_id="route-a",
        provider_kind=ProviderKind.LOCAL,
        model="model-a",
        timeout_seconds=2.0,
    )


def _assert_route_failure(error: ModelGatewayError) -> None:
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "route-a"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN


def test_hostile_response_identity_text_cannot_lie_through_route_equality() -> None:
    provider = _StaticProvider(
        ModelResponse(
            request_id=_AlwaysEqualText("foreign-request"),
            text="untrusted",
            provider_id=_AlwaysEqualText("foreign-provider"),
            provider_kind=ProviderKind.LOCAL,
            model="model-a",
        )
    )
    route = RoutedModelProvider(route_id="route-a", provider=provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(route.complete(_request()))

    assert provider.calls == 1
    _assert_route_failure(caught.value)


def test_model_response_subclass_fails_closed_at_route_boundary() -> None:
    provider = _StaticProvider(
        _ResponseSubclass(
            request_id="request-1",
            text="untrusted",
            provider_id="upstream-local",
            provider_kind=ProviderKind.LOCAL,
            model="model-a",
        )
    )
    route = RoutedModelProvider(route_id="route-a", provider=provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(route.complete(_request()))

    assert provider.calls == 1
    _assert_route_failure(caught.value)


def test_model_usage_subclass_fails_closed_at_route_boundary() -> None:
    provider = _StaticProvider(
        ModelResponse(
            request_id="request-1",
            text="untrusted",
            provider_id="upstream-local",
            provider_kind=ProviderKind.LOCAL,
            model="model-a",
            usage=_UsageSubclass(input_tokens=1, output_tokens=2, total_tokens=3),
        )
    )
    route = RoutedModelProvider(route_id="route-a", provider=provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(route.complete(_request()))

    assert provider.calls == 1
    _assert_route_failure(caught.value)


def test_valid_route_response_is_fresh_exact_snapshot_with_outer_route_identity() -> None:
    upstream_response = ModelResponse(
        request_id="request-1",
        text="answer",
        provider_id="upstream-local",
        provider_kind=ProviderKind.LOCAL,
        model="model-a",
        usage=ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5),
        latency_ms=12.5,
    )
    provider = _StaticProvider(upstream_response)
    route = RoutedModelProvider(route_id="route-a", provider=provider)

    result = asyncio.run(route.complete(_request()))

    assert provider.calls == 1
    assert result is not upstream_response
    assert type(result) is ModelResponse
    assert type(result.request_id) is str
    assert type(result.text) is str
    assert type(result.provider_id) is str
    assert type(result.model) is str
    assert type(result.provider_kind) is ProviderKind
    assert type(result.usage) is ModelUsage
    assert result.request_id == "request-1"
    assert result.provider_id == "route-a"
    assert result.provider_kind is ProviderKind.LOCAL
    assert result.model == "model-a"
    assert result.text == "answer"
    assert result.usage == ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5)
    assert result.usage is not upstream_response.usage
    assert result.latency_ms == 12.5
