from __future__ import annotations

import asyncio

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OpenAICompatibleProvider


_REQUESTED_MODEL = "synthetic-model-a"
_DIFFERENT_MODEL = "synthetic-model-b"


def _request(*, model: str = _REQUESTED_MODEL) -> ModelRequest:
    return ModelRequest(
        request_id="cloud-model-pinning-oracle",
        messages=(ModelMessage(role="user", content="synthetic prompt"),),
        model=model,
        provider_id="synthetic-cloud",
        timeout_seconds=2.0,
    )


def _provider(*, include_model: bool = True, response_model: object = _REQUESTED_MODEL):
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, object] = {
            "choices": [{"message": {"content": "synthetic response"}}],
        }
        if include_model:
            body["model"] = response_model
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    return OpenAICompatibleProvider(
        provider_id="synthetic-cloud",
        base_url="https://provider.example.test/v1",
        kind=ProviderKind.CLOUD,
        default_model="synthetic-provider-default",
        client_factory=client_factory,
    )


@pytest.mark.parametrize(
    ("include_model", "response_model"),
    (
        (False, None),
        (True, None),
        (True, ""),
        (True, " "),
        (True, 7),
        (True, ["synthetic-model-a"]),
    ),
)
def test_cloud_success_requires_provider_attested_model_identity(
    include_model: bool,
    response_model: object,
) -> None:
    provider = _provider(include_model=include_model, response_model=response_model)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "synthetic-cloud"


@pytest.mark.parametrize(
    "response_model",
    (
        _DIFFERENT_MODEL,
        "synthetic-model-a-resolved",
        "SYNTHETIC-MODEL-A",
    ),
)
def test_explicit_cloud_model_mismatch_fails_closed_before_success(
    response_model: str,
) -> None:
    provider = _provider(response_model=response_model)
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "synthetic-cloud"


def test_exact_cloud_model_identity_remains_accepted() -> None:
    provider = _provider(response_model=_REQUESTED_MODEL)
    gateway = ModelGateway()
    gateway.register(provider)

    response = asyncio.run(gateway.complete(_request()))

    assert response.model == _REQUESTED_MODEL
    assert response.provider_id == "synthetic-cloud"
