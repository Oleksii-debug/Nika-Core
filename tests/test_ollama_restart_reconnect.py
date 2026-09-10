from __future__ import annotations

import asyncio
import json
from enum import StrEnum
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
        self.chat_attempts = 0
        self.accepted_inferences = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.read())
        assert body["model"] == _MODEL
        self.chat_attempts += 1

        if self.state is _ServerState.DOWN:
            raise httpx.ConnectError("fixture Ollama server unavailable", request=request)

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


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="deterministic reconnect fixture"),),
        model=_MODEL,
        provider_id="ollama",
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


def test_ollama_recovers_on_later_explicit_request_after_nika_restart() -> None:
    server = _FakeOllamaServer()
    gateway, fallback = _new_gateway(server)

    first = asyncio.run(gateway.complete(_request("before-disconnect")))
    assert first.text == "reply-1"
    assert first.provider_id == "ollama"
    assert server.chat_attempts == 1
    assert server.accepted_inferences == 1

    server.state = _ServerState.DOWN
    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request("while-disconnected")))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "ollama"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert server.chat_attempts == 2
    assert server.accepted_inferences == 1
    assert fallback.calls == 0

    # A Nika restart discards all in-memory gateway/provider state. The external
    # fake server survives, just as a separately managed Ollama process would.
    server.state = _ServerState.AVAILABLE
    restarted_gateway, restarted_fallback = _new_gateway(server)
    recovered = asyncio.run(
        restarted_gateway.complete(_request("after-restart-and-reconnect"))
    )

    assert recovered.text == "reply-2"
    assert recovered.provider_id == "ollama"
    assert server.chat_attempts == 3
    assert server.accepted_inferences == 2
    assert fallback.calls == 0
    assert restarted_fallback.calls == 0


def test_uncertain_ollama_disconnect_is_not_retried_or_silently_rerouted() -> None:
    server = _FakeOllamaServer()
    server.state = _ServerState.DROP_AFTER_ACCEPT
    gateway, fallback = _new_gateway(server)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request("uncertain-disconnect")))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "ollama"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert server.chat_attempts == 1
    assert server.accepted_inferences == 1
    assert fallback.calls == 0
