from __future__ import annotations

import asyncio
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
from nika_core.model_gateway.providers import OpenAICompatibleProvider


def _provider(response_body: dict[str, object]) -> OpenAICompatibleProvider:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=response_body)
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return OpenAICompatibleProvider(
        provider_id="schema-test",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="test-model",
        client_factory=client_factory,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="schema-test",
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="schema-test",
    )


@pytest.mark.parametrize(
    "body",
    [
        {
            "model": "test-model",
            "choices": [{"message": {"content": None}}],
            "usage": {},
        },
        {
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": "2", "completion_tokens": 1, "total_tokens": 3},
        },
        {
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
        },
        {
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": [],
        },
    ],
)
def test_openai_compatible_invalid_schema_fails_closed(body: dict[str, object]) -> None:
    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(_provider(body).complete(_request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.provider_id == "schema-test"
    assert exc_info.value.retryable is False


def test_openai_compatible_missing_usage_remains_valid_optional_metadata() -> None:
    response = asyncio.run(
        _provider(
            {
                "model": "test-model",
                "choices": [{"message": {"content": "ok"}}],
            }
        ).complete(_request())
    )

    assert response.text == "ok"
    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.total_tokens is None
