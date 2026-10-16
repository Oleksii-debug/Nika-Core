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


def _response_body(*, role: object = "assistant") -> dict[str, object]:
    return {
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": role, "content": "synthetic completed answer"},
            }
        ],
    }


def _complete(body: object):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=body))

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OpenAICompatibleProvider(
        provider_id="test-api",
        base_url="https://models.example.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="test-model",
        client_factory=client_factory,
    )
    request = ModelRequest(
        request_id="semantic-envelope",
        provider_id="test-api",
        messages=(ModelMessage(role="user", content="Привіт"),),
    )
    return asyncio.run(provider.complete(request))


@pytest.mark.parametrize(
    "semantic_error",
    (
        "provider failed",
        {"message": "private-canary"},
        None,
        False,
        0,
    ),
)
def test_http_200_with_error_member_is_never_a_success(semantic_error: object) -> None:
    body = {**_response_body(), "error": semantic_error}
    with pytest.raises(ModelGatewayError) as caught:
        _complete(body)
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "test-api"
    assert caught.value.retryable is False
    assert "private-canary" not in repr(caught.value)
    assert "private-canary" not in repr(caught.value.__cause__)


@pytest.mark.parametrize(
    "role",
    ("user", "system", "tool", None, False, 12),
)
def test_explicit_nonassistant_role_is_never_a_completed_answer(role: object) -> None:
    with pytest.raises(ModelGatewayError) as caught:
        _complete(_response_body(role=role))
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "test-api"
    assert caught.value.retryable is False


def test_explicit_assistant_response_remains_usable() -> None:
    answer = _complete(_response_body())
    assert answer.text == "synthetic completed answer"
    assert answer.model == "test-model"


def test_legacy_response_with_omitted_role_remains_usable() -> None:
    body = _response_body()
    del body["choices"][0]["message"]["role"]
    answer = _complete(body)
    assert answer.text == "synthetic completed answer"
    assert answer.model == "test-model"
