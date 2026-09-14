from __future__ import annotations

import asyncio

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)


class _RecordingResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return "synthetic-route-snapshot-secret"


class _TextSubclass(str):
    pass


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="route-snapshot-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="model-a",
        provider_id="approved-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def test_provider_uses_constructor_route_snapshot_after_caller_mutation() -> None:
    resolver = _RecordingResolver()
    transport_calls = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        assert request.url.host == "api.example.test"
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "snapshot response"}}],
            },
        )

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
            timeout=timeout,
        )

    config = ApiModelRouteConfig(
        provider_id="approved-api",
        base_url="https://api.example.test/v1",
        default_model="model-a",
        credential_ref="env:NIKA_ROUTE_A",
        supports_private_data=False,
        supports_hard_cancellation=False,
    )
    provider = CredentialRefOpenAICompatibleProvider(
        config=config,
        credential_resolver=resolver,
        client_factory=client_factory,
    )

    object.__setattr__(config, "provider_id", "substituted-api")
    object.__setattr__(config, "base_url", "https://other.example.test/v1")
    object.__setattr__(config, "default_model", "model-b")
    object.__setattr__(config, "credential_ref", "env:NIKA_ROUTE_B")
    object.__setattr__(config, "supports_private_data", True)
    object.__setattr__(config, "supports_hard_cancellation", True)

    capabilities = provider.capabilities
    assert capabilities.provider_id == "approved-api"
    assert capabilities.effect_network_host == "api.example.test"
    assert capabilities.supports_private_data is False
    assert capabilities.supports_hard_cancellation is False
    assert provider.credential_ref == "env:NIKA_ROUTE_A"

    response = asyncio.run(provider.complete(_request()))

    assert response.provider_id == "approved-api"
    assert response.model == "model-a"
    assert response.text == "snapshot response"
    assert resolver.references == ["env:NIKA_ROUTE_A"]
    assert transport_calls == 1


def test_provider_rejects_behavioral_route_identity_scalar() -> None:
    config = ApiModelRouteConfig(
        provider_id=_TextSubclass("approved-api"),
        base_url="https://api.example.test/v1",
        default_model="model-a",
        credential_ref="env:NIKA_ROUTE_A",
    )

    with pytest.raises(TypeError, match="provider_id must be exact text"):
        CredentialRefOpenAICompatibleProvider(
            config=config,
            credential_resolver=_RecordingResolver(),
        )
