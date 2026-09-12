from __future__ import annotations

import asyncio
from enum import StrEnum
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
from nika_core.model_gateway.providers import OllamaProvider


_MODEL = "fixture-model"


class _ServerState(StrEnum):
    AVAILABLE = "available"
    DOWN = "down"
    DROP_AFTER_ACCEPT = "drop_after_accept"


class _FakeOllamaServer:
    def __init__(self) -> None:
        self.state = _ServerState.AVAILABLE
        self.http_attempts = 0
        self.chat_attempts = 0
        self.accepted_inferences = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.http_attempts += 1
        if self.state is _ServerState.DOWN:
            raise httpx.ConnectError("fixture Ollama server unavailable", request=request)

        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "fixture-version"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": _MODEL,
                            "model": _MODEL,
                            "modified_at": "2026-01-01T00:00:00Z",
                            "size": 1,
                            "digest": "fixture-digest",
                            "details": {},
                        }
                    ]
                },
            )

        assert request.url.path == "/api/chat"
        body = json.loads(request.read())
        assert body["model"] == _MODEL
        self.chat_attempts += 1
        self.accepted_inferences += 1

        if self.state is _ServerState.DROP_AFTER_ACCEPT:
            raise httpx.ReadError("fixture connection dropped after accept", request=request)

        return httpx.Response(
            200,
            json={
                "model": _MODEL,
                "message": {
                    "role": "assistant",
                    "content": f"reply-{self.accepted_inferences}",
                },
                "done": True,
            },
        )


class _NeverSelectedFallback:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="registered-fallback",
            kind=ProviderKind.NO_LLM,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest):
        self.calls += 1
        raise AssertionError(f"unexpected silent fallback for {request.request_id}")


def _request(request_id: str, *, explicit_fallback: bool = False) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="deterministic reconnect fixture"),),
        model=_MODEL,
        provider_id="ollama",
        fallback_provider_ids=("registered-fallback",) if explicit_fallback else (),
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=1,
        temperature=0,
    )


def _new_gateway(server: _FakeOllamaServer) -> tuple[ModelGateway, _NeverSelectedFallback]:
    transport = httpx.MockTransport(server.handle)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    fallback = _NeverSelectedFallback()
    gateway = ModelGateway()
    gateway.register(OllamaProvider(default_model=_MODEL, client_factory=client_factory))
    gateway.register(fallback)
    return gateway, fallback


@pytest.mark.parametrize("restart_nika", (False, True), ids=("same-runtime", "after-nika-restart"))
def test_ollama_recovers_only_on_a_later_explicit_request(restart_nika: bool) -> None:
    server = _FakeOllamaServer()
    gateway, fallback = _new_gateway(server)
    fallbacks = [fallback]

    first = asyncio.run(gateway.complete(_request("before-disconnect")))
    assert first.text == "reply-1"
    assert first.provider_id == "ollama"
    assert server.chat_attempts == 1
    assert server.accepted_inferences == 1

    server.state = _ServerState.DOWN
    http_attempts_before_failure = server.http_attempts
    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request("while-disconnected")))

    # Exact server-availability taxonomy belongs to the model-presence lane. DEV24
    # only requires a typed Nika-owned terminal failure and no hidden replay/route.
    error = exc_info.value
    assert isinstance(error.code, ModelErrorCode)
    assert error.provider_id == "ollama"
    assert server.http_attempts == http_attempts_before_failure + 1
    assert server.chat_attempts == 1
    assert server.accepted_inferences == 1
    assert all(item.calls == 0 for item in fallbacks)

    if restart_nika:
        # Reconstruct the in-memory AI boundary while Ollama is still unavailable.
        gateway, fallback = _new_gateway(server)
        fallbacks.append(fallback)

    server.state = _ServerState.AVAILABLE
    recovered = asyncio.run(gateway.complete(_request("after-reconnect")))

    assert recovered.text == "reply-2"
    assert recovered.provider_id == "ollama"
    assert server.chat_attempts == 2
    assert server.accepted_inferences == 2
    assert all(item.calls == 0 for item in fallbacks)


def test_uncertain_ollama_disconnect_is_not_retried_or_rerouted() -> None:
    server = _FakeOllamaServer()
    server.state = _ServerState.DROP_AFTER_ACCEPT
    gateway, fallback = _new_gateway(server)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request("uncertain-disconnect", explicit_fallback=True)
            )
        )

    error = exc_info.value
    assert isinstance(error.code, ModelErrorCode)
    assert error.provider_id == "ollama"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert server.chat_attempts == 1
    assert server.accepted_inferences == 1
    assert fallback.calls == 0
