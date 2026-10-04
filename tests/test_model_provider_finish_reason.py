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


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="completion-termination",
        provider_id="test-api",
        messages=(ModelMessage(role="user", content="complete the answer"),),
    )


def _response(
    *,
    include_reason: bool,
    reason: object = "stop",
    model: object = "test-model",
    include_model: bool = True,
) -> httpx.Response:
    choice: dict[str, object] = {"message": {"content": "possible partial text"}}
    if include_reason:
        choice["finish_reason"] = reason
    body: dict[str, object] = {"choices": [choice]}
    if include_model:
        body["model"] = model
    return httpx.Response(200, json=body)


def _factory(response: httpx.Response):
    transport = httpx.MockTransport(lambda _request: response)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return client_factory


def _provider(response: httpx.Response) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_id="test-api",
        base_url="https://models.example.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="test-model",
        client_factory=_factory(response),
    )


@pytest.mark.parametrize(
    "reason",
    (
        "length",
        "content_filter",
        "tool_calls",
        "function_call",
        "unknown",
        None,
        False,
        0,
    ),
)
def test_explicit_nonterminal_reason_never_becomes_success(reason: object) -> None:
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _provider(_response(include_reason=True, reason=reason)).complete(
                _request()
            )
        )

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "test-api"
    assert caught.value.retryable is False
    assert "possible partial text" not in str(caught.value)


@pytest.mark.parametrize("include_reason", (True, False))
def test_terminal_or_legacy_omitted_reason_still_works(
    include_reason: bool,
) -> None:
    response = asyncio.run(
        _provider(_response(include_reason=include_reason)).complete(_request())
    )
    assert response.text == "possible partial text"
    assert response.model == "test-model"


def test_credential_reference_route_inherits_terminal_reason_gate() -> None:
    class Resolver:
        def resolve(self, credential_ref: str) -> str:
            assert credential_ref == "env:TEST_API_KEY"
            return "synthetic-canary-not-to-log"

    provider = CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="test-api",
            base_url="https://models.example.invalid/v1",
            default_model="test-model",
            credential_ref="env:TEST_API_KEY",
        ),
        credential_resolver=Resolver(),
        client_factory=_factory(_response(include_reason=True, reason="length")),
    )
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is False
    assert caught.value.__cause__ is None
    assert "synthetic-canary-not-to-log" not in repr(caught.value)


@pytest.mark.parametrize(
    ("include_model", "raw_model"),
    (
        (False, "test-model"),
        (True, None),
        (True, ""),
        (True, " "),
        (True, " test-model"),
        (True, "test-model "),
        (True, 7),
        (True, True),
        (True, ["test-model"]),
    ),
)
def test_success_requires_attested_nonblank_model_identity(
    include_model: bool, raw_model: object
) -> None:
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            _provider(
                _response(
                    include_reason=True,
                    include_model=include_model,
                    model=raw_model,
                )
            ).complete(_request())
        )
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "test-api"
    assert caught.value.retryable is False
