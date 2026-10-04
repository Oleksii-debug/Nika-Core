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


class _CloudProvider:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="cloud-test",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
            effect_network_host="api.example.test",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="Готово 🧠",
            provider_id="cloud-test",
            provider_kind=ProviderKind.CLOUD,
            model=request.model or "fixture-model",
        )


class _MutatingAuthorizer:
    def __init__(self, field: str, value: object, *, result: object = None) -> None:
        self.field = field
        self.value = value
        self.result = result
        self.calls = 0
        self.seen: list[ProviderCapabilities] = []

    def authorize_cloud_effect(
        self, *, request: ModelRequest, provider: ProviderCapabilities
    ) -> None:
        self.calls += 1
        self.seen.append(provider)
        object.__setattr__(provider, self.field, self.value)
        return self.result  # type: ignore[return-value]


def _request(privacy: PrivacyClass) -> ModelRequest:
    return ModelRequest(
        request_id="capability-approval",
        provider_id="cloud-test",
        model="fixture-model",
        messages=(ModelMessage(role="user", content="Привіт 🧠"),),
        privacy=privacy,
    )


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("provider_id", "swapped-provider"),
        ("kind", ProviderKind.LOCAL),
        ("effect_network_host", "other.example.test"),
        ("supports_private_data", True),
        ("supports_hard_cancellation", True),
    ),
)
def test_mutating_authorizer_cannot_poison_provider_or_next_private_route(
    field: str, mutated: object
) -> None:
    provider = _CloudProvider()
    approval = _MutatingAuthorizer(field, mutated)
    gateway = ModelGateway(cloud_effect_authorizer=approval)
    gateway.register(provider)

    response = asyncio.run(gateway.complete(_request(PrivacyClass.PUBLIC)))

    assert response.provider_id == "cloud-test"
    assert response.provider_kind is ProviderKind.CLOUD
    assert response.text == "Готово 🧠"
    assert provider.calls == 1
    assert approval.calls == 1
    assert getattr(approval.seen[0], field) == mutated
    assert getattr(gateway._providers["cloud-test"].capabilities, field) != mutated
    assert approval.seen[0] is not gateway._providers["cloud-test"].capabilities

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(PrivacyClass.PRIVATE)))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert provider.calls == 1
    assert approval.calls == 1


def test_invalid_cloud_authorization_does_not_poison_registered_capabilities() -> None:
    provider = _CloudProvider()
    approval = _MutatingAuthorizer("supports_private_data", True, result=False)
    gateway = ModelGateway(cloud_effect_authorizer=approval)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(PrivacyClass.PUBLIC)))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.calls == 0
    assert gateway._providers["cloud-test"].capabilities.supports_private_data is False

    with pytest.raises(ModelGatewayError) as private:
        asyncio.run(gateway.complete(_request(PrivacyClass.PRIVATE)))

    assert private.value.code is ModelErrorCode.INVALID_REQUEST
    assert provider.calls == 0
    assert approval.calls == 1
