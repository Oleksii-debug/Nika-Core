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
)
from nika_core.model_gateway.providers import OllamaProvider


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="ollama-protocol-validation",
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="ollama",
        privacy=PrivacyClass.PRIVATE,
        model="model-a",
        temperature=0,
    )


def _completion(
    *,
    model: object = "model-a",
    content: object = "answer",
    done: object = True,
    prompt_eval_count: object = 7,
    eval_count: object = 3,
) -> dict[str, object]:
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }


def _run_response(response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda _request: response)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(
        default_model="model-a",
        client_factory=client_factory,
    )
    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "ollama"
    assert error.retryable is False


@pytest.mark.parametrize(
    "response",
    (
        pytest.param(
            httpx.Response(
                200,
                content=b'{"model":"model-a"',
                headers={"content-type": "application/json"},
            ),
            id="invalid-json",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "model": "model-a",
                    "message": {"role": "assistant"},
                    "done": True,
                },
            ),
            id="missing-response-text",
        ),
        pytest.param(
            httpx.Response(200, json=["unexpected", "array"]),
            id="unexpected-top-level-object",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_completion(prompt_eval_count=True),
            ),
            id="usage-bool",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_completion(prompt_eval_count="7"),
            ),
            id="usage-string",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_completion(eval_count=-1),
            ),
            id="usage-negative",
        ),
    ),
)
def test_ollama_existing_malformed_protocol_cases_fail_closed(
    response: httpx.Response,
) -> None:
    _run_response(response)


@pytest.mark.parametrize(
    "response",
    (
        pytest.param(
            httpx.Response(
                200,
                content=json.dumps(_completion()).encode("utf-8"),
                headers={"content-type": "text/plain"},
            ),
            id="wrong-content-type",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "answer"},
                    "done": True,
                },
            ),
            id="missing-model",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_completion(model="model-b"),
            ),
            id="wrong-model",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_completion(done=False),
            ),
            id="truncated-streaming-item",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "model": "model-a",
                    "message": {"role": "assistant", "content": "answer"},
                },
            ),
            id="missing-done",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    **_completion(),
                    "error": "model failed after response started",
                },
            ),
            id="http-200-semantic-error",
        ),
    ),
)
def test_ollama_nonstreaming_protocol_rejects_false_success(
    response: httpx.Response,
) -> None:
    _run_response(response)
