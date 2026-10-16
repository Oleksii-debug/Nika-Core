from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ProviderKind,
)
from nika_core.model_gateway.providers import (
    OllamaProvider,
    OpenAICompatibleProvider,
)


def _request(provider_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=f"converged-{provider_id}",
        provider_id=provider_id,
        messages=(ModelMessage(role="user", content="Привіт"),),
    )


def _cloud(body: object) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == "synthetic-cloud-model"
        return httpx.Response(200, json=body)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return OpenAICompatibleProvider(
        provider_id="cloud",
        base_url="https://provider.example.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="synthetic-cloud-model",
        client_factory=factory,
    )


def _ollama(body: object) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        assert request.url.host in {"localhost", "127.0.0.1", "::1"}
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3:8b"
        assert payload["stream"] is False
        assert payload["think"] is False
        return httpx.Response(200, json=body)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return OllamaProvider(default_model="qwen3:8b", client_factory=factory)


def test_combined_cloud_and_local_providers_complete_without_network() -> None:
    async def scenario() -> None:
        cloud = await _cloud(
            {
                "model": "synthetic-cloud-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Хмара"},
                    }
                ],
            }
        ).complete(_request("cloud"))
        local = await _ollama(
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "Локально"},
                "done": True,
                "done_reason": "stop",
            }
        ).complete(_request("ollama"))
        assert (cloud.text, cloud.model) == ("Хмара", "synthetic-cloud-model")
        assert (local.text, local.model) == ("Локально", "qwen3:8b")
        assert cloud.provider_kind is ProviderKind.CLOUD
        assert local.provider_kind is ProviderKind.LOCAL

    asyncio.run(scenario())


def test_combined_cloud_and_local_providers_fail_closed_without_network() -> None:
    async def scenario() -> None:
        cloud = _cloud(
            {
                "model": "synthetic-cloud-model",
                "error": {"message": "private-canary"},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "untrusted"},
                    }
                ],
            }
        )
        local = _ollama(
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "partial"},
                "done": False,
            }
        )
        for provider, provider_id in ((cloud, "cloud"), (local, "ollama")):
            with pytest.raises(ModelGatewayError) as caught:
                await provider.complete(_request(provider_id))
            assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
            assert caught.value.provider_id == provider_id
            assert caught.value.retryable is False
            assert "private-canary" not in repr(caught.value)

    asyncio.run(scenario())
