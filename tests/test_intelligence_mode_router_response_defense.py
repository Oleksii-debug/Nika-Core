from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModeError,
    IntelligenceModeErrorCode,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class BypassingGateway(ModelGateway):
    """Return malformed success metadata directly so router defenses are exercised."""

    def __init__(
        self,
        *,
        response_provider_id: str | None = None,
        response_kind: ProviderKind | None = None,
        response_request_id: str | None = None,
    ) -> None:
        super().__init__()
        self._response_provider_id = response_provider_id
        self._response_kind = response_kind
        self._response_request_id = response_request_id
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        assert request.provider_id is not None
        assert request.provider_kind is not None
        return ModelResponse(
            request_id=self._response_request_id or request.request_id,
            text="ok",
            provider_id=self._response_provider_id or request.provider_id,
            provider_kind=self._response_kind or request.provider_kind,
            model=request.model or "test-model",
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="private task"),),
        provider_id="untrusted-route",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("untrusted-fallback",),
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=12.0,
    )


def test_router_rejects_provider_kind_substitution_even_if_gateway_is_bypassed() -> None:
    gateway = BypassingGateway(response_kind=ProviderKind.CLOUD)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(
            router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, _request())
        )

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH
    assert gateway.requests[0].provider_id == "foundry-local"
    assert gateway.requests[0].provider_kind is ProviderKind.LOCAL
    assert gateway.requests[0].fallback_provider_ids == ()


def test_router_rejects_provider_identity_substitution_even_if_gateway_is_bypassed() -> None:
    gateway = BypassingGateway(response_provider_id="other-cloud")
    router = IntelligenceModeRouter(
        gateway=gateway,
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH
    assert gateway.requests[0].provider_id == "approved-cloud"
    assert gateway.requests[0].provider_kind is ProviderKind.CLOUD
    assert gateway.requests[0].fallback_provider_ids == ()


def test_router_rejects_request_identity_substitution_even_if_gateway_is_bypassed() -> None:
    gateway = BypassingGateway(response_request_id="other-request")
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH
    assert gateway.requests[0].provider_id == "ollama"
    assert gateway.requests[0].provider_kind is ProviderKind.LOCAL
    assert gateway.requests[0].fallback_provider_ids == ()
