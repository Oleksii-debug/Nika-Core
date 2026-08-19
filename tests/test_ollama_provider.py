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
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import (
    DeterministicMockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
)


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "ollama-test",
        "messages": (ModelMessage(role="user", content="hello"),),
        "provider_id": "ollama",
        "privacy": PrivacyClass.PRIVATE,
        "temperature": 0,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def test_ollama_uses_native_chat_api_without_streaming_or_thinking() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": "answer"},
                "done": True,
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(default_model="qwen3:8b", client_factory=client_factory)
    )

    response = asyncio.run(gateway.complete(_request()))

    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"] == {
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    assert response.text == "answer"
    assert response.provider_id == "ollama"
    assert response.provider_kind is ProviderKind.LOCAL
    assert response.model == "qwen3:8b"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 10


def test_ollama_request_model_overrides_default() -> None:
    seen_model: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        seen_model.append(body["model"])
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(default_model="qwen3:8b", client_factory=client_factory)
    response = asyncio.run(provider.complete(_request(model="other-model")))

    assert seen_model == ["other-model"]
    assert response.model == "other-model"


def test_ollama_http_failure_is_normalized() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, json={"error": "busy"})
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(default_model="qwen3:8b", client_factory=client_factory)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.RATE_LIMITED
    assert exc_info.value.retryable is True
    assert exc_info.value.provider_id == "ollama"


def test_ollama_invalid_native_schema_fails_closed() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"model": "qwen3:8b", "done": True})
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(default_model="qwen3:8b", client_factory=client_factory)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.retryable is False


def test_ollama_does_not_claim_hard_server_side_cancellation() -> None:
    provider = OllamaProvider(default_model="qwen3:8b")

    assert provider.capabilities.supports_hard_cancellation is False
    assert provider.capabilities.supports_private_data is True


def test_generic_http_provider_does_not_claim_hard_cancellation_by_default() -> None:
    provider = OpenAICompatibleProvider(
        provider_id="remote",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="model",
    )

    assert provider.capabilities.supports_hard_cancellation is False


def test_http_provider_can_opt_in_only_when_adapter_has_external_proof() -> None:
    provider = OpenAICompatibleProvider(
        provider_id="proven-cancellable",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="model",
        supports_hard_cancellation=True,
    )

    assert provider.capabilities.supports_hard_cancellation is True


def test_provider_reported_timeout_cannot_fallback_without_hard_cancellation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    gateway = ModelGateway()
    gateway.register(OllamaProvider(default_model="qwen3:8b", client_factory=client_factory))
    gateway.register(DeterministicMockProvider(provider_id="fallback"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(fallback_provider_ids=("fallback",), timeout_seconds=5)
            )
        )

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.provider_id == "ollama"
    assert exc_info.value.retryable is False


def test_ollama_constructor_rejects_empty_identity_inputs() -> None:
    with pytest.raises(ValueError, match="default_model"):
        OllamaProvider(default_model=" ")

    with pytest.raises(ValueError, match="base_url"):
        OllamaProvider(default_model="qwen3:8b", base_url=" ")
