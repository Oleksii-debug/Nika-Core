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
)
from nika_core.model_gateway.providers import OllamaProvider


def _complete(response: httpx.Response):
    transport = httpx.MockTransport(lambda _request: response)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(default_model="qwen3:8b", client_factory=client_factory)
    request = ModelRequest(
        request_id="terminal-ollama",
        provider_id="ollama",
        messages=(ModelMessage(role="user", content="Привіт"),),
    )
    return asyncio.run(provider.complete(request))


def _body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": "Вітаю"},
        "done": True,
    }
    body.update(changes)
    return body


@pytest.mark.parametrize(
    "reason",
    (
        "length",
        "unload",
        None,
        False,
    ),
)
def test_nonterminal_done_reason_is_not_success(reason: object) -> None:
    with pytest.raises(ModelGatewayError) as caught:
        _complete(httpx.Response(200, json=_body(done_reason=reason)))
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is False


@pytest.mark.parametrize("role", ("user", "tool", None))
def test_untrusted_message_role_is_not_assistant_response(role: object) -> None:
    with pytest.raises(ModelGatewayError) as caught:
        _complete(
            httpx.Response(
                200,
                json=_body(message={"role": role, "content": "not assistant"}),
            )
        )
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR


@pytest.mark.parametrize("reason", ("stop", "omitted"))
def test_completed_unicode_answer_and_json_charset_succeed(reason: str) -> None:
    body = _body()
    if reason != "omitted":
        body["done_reason"] = reason
    response = httpx.Response(
        200,
        json=body,
        headers={"content-type": "application/json; charset=utf-8"},
    )
    result = _complete(response)
    assert result.text == "Вітаю"
    assert result.model == "qwen3:8b"
