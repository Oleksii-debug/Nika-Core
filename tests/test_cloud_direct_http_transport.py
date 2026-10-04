from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ProviderKind,
)
from nika_core.model_gateway.providers import OpenAICompatibleProvider


class _Resolver:
    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == "env:NIKA_TEST_CLOUD_TOKEN"
        return "synthetic-test-token"


@pytest.mark.parametrize("credential_ref_route", (False, True))
def test_default_cloud_client_disables_inherited_proxies_and_redirects(
    monkeypatch: pytest.MonkeyPatch, credential_ref_route: bool
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.example.test:8080")
    monkeypatch.setenv("ALL_PROXY", "http://untrusted-proxy.example.test:8080")
    actual_client = httpx.AsyncClient
    client_options: list[dict[str, Any]] = []
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host or "")
        assert request.url.host == "authorized.example.test"
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [
                    {"finish_reason": "stop", "message": {
                        "role": "assistant", "content": "Пряма відповідь",
                    }}
                ],
            },
        )

    def capture_client(**options: Any) -> httpx.AsyncClient:
        client_options.append(options)
        return actual_client(
            transport=httpx.MockTransport(handler),
            **options,
        )

    monkeypatch.setattr(httpx, "AsyncClient", capture_client)

    if credential_ref_route:
        provider = CredentialRefOpenAICompatibleProvider(
            config=ApiModelRouteConfig(
                provider_id="cloud-test",
                base_url="https://authorized.example.test/v1",
                default_model="model-a",
                credential_ref="env:NIKA_TEST_CLOUD_TOKEN",
            ),
            credential_resolver=_Resolver(),
        )
    else:
        provider = OpenAICompatibleProvider(
            provider_id="cloud-test",
            base_url="https://authorized.example.test/v1",
            kind=ProviderKind.CLOUD,
            default_model="model-a",
        )
    request = ModelRequest(
        request_id="direct-transport",
        messages=(ModelMessage(role="user", content="Привіт"),),
        model="model-a",
        provider_id="cloud-test",
        provider_kind=ProviderKind.CLOUD,
    )

    response = asyncio.run(provider.complete(request))

    assert response.text == "Пряма відповідь"
    assert reached == ["authorized.example.test"]
    assert len(client_options) == 1
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False
    assert 0 < client_options[0]["timeout"] <= request.timeout_seconds


def test_default_cloud_client_does_not_follow_credential_bearing_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_client = httpx.AsyncClient
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host or "")
        return httpx.Response(
            307, headers={"Location": "https://unapproved.example.test/v1/chat/completions"}
        )

    def capture_client(**options: Any) -> httpx.AsyncClient:
        return actual_client(transport=httpx.MockTransport(handler), **options)

    monkeypatch.setattr(httpx, "AsyncClient", capture_client)
    provider = CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="cloud-test",
            base_url="https://authorized.example.test/v1",
            default_model="model-a",
            credential_ref="env:NIKA_TEST_CLOUD_TOKEN",
        ),
        credential_resolver=_Resolver(),
    )
    request = ModelRequest(
        request_id="no-redirect",
        messages=(ModelMessage(role="user", content="Привіт"),),
        provider_id="cloud-test",
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(request))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert reached == ["authorized.example.test"]
