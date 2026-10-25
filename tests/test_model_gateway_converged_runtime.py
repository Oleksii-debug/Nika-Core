from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider, OpenAICompatibleProvider


class _CloudApproval:
    def __init__(self) -> None:
        self.calls = 0

    def authorize_cloud_effect(
        self, *, request: ModelRequest, provider: ProviderCapabilities
    ) -> None:
        assert request.model == "cloud-model"
        assert provider.kind is ProviderKind.CLOUD
        assert provider.provider_id == "cloud"
        self.calls += 1


def _request(provider_id: str, model: str, kind: ProviderKind) -> ModelRequest:
    return ModelRequest(
        request_id=f"integrated-{provider_id}",
        messages=(ModelMessage(role="user", content="Привіт"),),
        model=model,
        provider_id=provider_id,
        provider_kind=kind,
        privacy=PrivacyClass.PUBLIC if kind is ProviderKind.CLOUD else PrivacyClass.PRIVATE,
        timeout_seconds=2.0,
    )


def _local(body: object, calls: list[str]) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.host == "localhost"
        assert request.url.path == "/api/chat"
        assert json.loads(request.content)["model"] == "qwen3:8b"
        return httpx.Response(200, json=body)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return OllamaProvider(default_model="qwen3:8b", client_factory=factory)


def _cloud(body: object, calls: list[str]) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == "cloud-model"
        return httpx.Response(200, json=body)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return OpenAICompatibleProvider(
        provider_id="cloud",
        base_url="https://provider.example.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="cloud-model",
        client_factory=factory,
    )


@pytest.mark.parametrize("done", (True, False))
def test_native_ollama_terminal_evidence_crosses_real_gateway(done: bool) -> None:
    calls: list[str] = []
    gateway = ModelGateway()
    gateway.register(
        _local(
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "Локальна відповідь"},
                "done": done,
            },
            calls,
        )
    )
    request = _request("ollama", "qwen3:8b", ProviderKind.LOCAL)

    if done:
        response = asyncio.run(gateway.complete(request))
        assert response.request_id == request.request_id
        assert response.provider_id == "ollama"
        assert response.provider_kind is ProviderKind.LOCAL
        assert response.model == "qwen3:8b"
        assert response.text == "Локальна відповідь"
    else:
        with pytest.raises(ModelGatewayError) as caught:
            asyncio.run(gateway.complete(request))
        assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
        assert caught.value.retryable is False
    assert len(calls) == 1


def test_cloud_request_without_current_authority_has_no_network_effect() -> None:
    calls: list[str] = []
    gateway = ModelGateway()
    gateway.register(
        _cloud(
            {
                "model": "cloud-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "not authorized"},
                    }
                ],
            },
            calls,
        )
    )
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request("cloud", "cloud-model", ProviderKind.CLOUD)))
    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert calls == []


@pytest.mark.parametrize("response_mode", ("completed", "substituted", "semantic_error"))
def test_authorized_cloud_provider_and_gateway_jointly_validate_success(
    response_mode: str,
) -> None:
    calls: list[str] = []
    body: dict[str, object] = {
        "model": "other-model" if response_mode == "substituted" else "cloud-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Хмарна відповідь"},
            }
        ],
    }
    if response_mode == "semantic_error":
        body["error"] = {"message": "private-canary"}
    approval = _CloudApproval()
    gateway = ModelGateway(cloud_effect_authorizer=approval)
    gateway.register(_cloud(body, calls))
    request = _request("cloud", "cloud-model", ProviderKind.CLOUD)

    if response_mode == "completed":
        result = asyncio.run(gateway.complete(request))
        assert result.request_id == request.request_id
        assert result.provider_id == "cloud"
        assert result.provider_kind is ProviderKind.CLOUD
        assert result.model == "cloud-model"
        assert result.text == "Хмарна відповідь"
    else:
        with pytest.raises(ModelGatewayError) as caught:
            asyncio.run(gateway.complete(request))
        assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
        assert caught.value.retryable is False
        assert "private-canary" not in str(caught.value)
    assert approval.calls == 1
    assert len(calls) == 1
