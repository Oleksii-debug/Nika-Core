from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nika_core.model_gateway import api_route
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OpenAICompatibleProvider

_SECRET = "synthetic-cancellation-test-credential"
_REF = "env:NIKA_CANCEL_TEST_ONLY"


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="cloud-cancellation-fence",
        messages=(ModelMessage(role="user", content="Привіт"),),
        provider_id="cloud-cancel-test",
        model="model-a",
        timeout_seconds=2.0,
    )


class _Resolver:
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _REF
        self.calls += 1
        if self.cancel:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return _SECRET


def _wrapped(resolver: _Resolver, factory: Any) -> CredentialRefOpenAICompatibleProvider:
    return CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="cloud-cancel-test",
            base_url="https://approved.example.test/v1",
            default_model="model-a",
            credential_ref=_REF,
        ),
        credential_resolver=resolver,
        client_factory=factory,
    )


def _cancel_now() -> None:
    task = asyncio.current_task()
    assert task is not None
    task.cancel()


class _NoEffectClient:
    def __init__(self, events: list[str], *, cancel_on_enter: bool = False) -> None:
        self.events = events
        self.cancel_on_enter = cancel_on_enter

    async def __aenter__(self) -> _NoEffectClient:
        self.events.append("enter")
        if self.cancel_on_enter:
            _cancel_now()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.events.append("exit")

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        self.events.append("post")
        raise AssertionError("cancelled request must not start an HTTP effect")


def test_resolver_cancellation_prevents_provider_construction_and_http() -> None:
    resolver = _Resolver(cancel=True)
    events: list[str] = []

    def no_client(**_kwargs: Any) -> _NoEffectClient:
        events.append("factory")
        return _NoEffectClient(events)

    provider = _wrapped(resolver, no_client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.complete(_request()))

    assert resolver.calls == 1
    assert events == []


def test_constructor_cancellation_prevents_client_and_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = _wrapped(_Resolver(), lambda **_kwargs: _NoEffectClient(events))
    original = OpenAICompatibleProvider

    class CancellingProvider(original):
        def __init__(self, **kwargs: Any) -> None:
            if kwargs.get("api_key") is not None:
                _cancel_now()
            super().__init__(**kwargs)

    monkeypatch.setattr(api_route, "OpenAICompatibleProvider", CancellingProvider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.complete(_request()))

    assert events == []


def test_client_factory_cancellation_prevents_http() -> None:
    events: list[str] = []

    def factory(**_kwargs: Any) -> _NoEffectClient:
        events.append("factory")
        _cancel_now()
        return _NoEffectClient(events)

    provider = OpenAICompatibleProvider(
        provider_id="cloud-cancel-test",
        base_url="https://approved.example.test/v1",
        kind=ProviderKind.CLOUD,
        default_model="model-a",
        client_factory=factory,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.complete(_request()))

    assert events == ["factory", "enter", "exit"]


def test_client_enter_cancellation_prevents_http() -> None:
    events: list[str] = []
    provider = OpenAICompatibleProvider(
        provider_id="cloud-cancel-test",
        base_url="https://approved.example.test/v1",
        kind=ProviderKind.CLOUD,
        default_model="model-a",
        client_factory=lambda **_kwargs: _NoEffectClient(
            events, cancel_on_enter=True
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.complete(_request()))

    assert events == ["enter", "exit"]


def test_post_effect_cancellation_cannot_publish_success() -> None:
    effects: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        effects.append(request.url.host or "")
        _cancel_now()
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Готово"},
                }],
            },
        )

    provider = OpenAICompatibleProvider(
        provider_id="cloud-cancel-test",
        base_url="https://approved.example.test/v1",
        kind=ProviderKind.CLOUD,
        default_model="model-a",
        client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.complete(_request()))

    # An effect already begun is UNKNOWN, not a claim of NO_EFFECT.
    assert effects == ["approved.example.test"]


def test_gateway_audits_cancel_without_success_or_fallback() -> None:
    events: list[str] = []

    class Audit:
        def append(self, *, event_type: str, **_kwargs: Any) -> int:
            events.append(event_type)
            return len(events)

    class Authorizer:
        def authorize_cloud_effect(self, **_kwargs: Any) -> None:
            return None

    provider = _wrapped(
        _Resolver(cancel=True),
        lambda **_kwargs: _NoEffectClient(events),
    )
    gateway = ModelGateway(audit_log=Audit(), cloud_effect_authorizer=Authorizer())
    gateway.register(provider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gateway.complete(_request()))

    assert events == ["model.requested", "model.cancelled"]


def test_uncancelled_cloud_request_still_completes() -> None:
    effects: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        effects.append(request.url.host or "")
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Готово"},
                }],
            },
        )

    provider = _wrapped(
        _Resolver(),
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    response = asyncio.run(provider.complete(_request()))
    assert response.text == "Готово"
    assert response.model == "model-a"
    assert effects == ["approved.example.test"]
