from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from nika_core.model_gateway import api_route
from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OpenAICompatibleProvider

_SECRET = "synthetic-secret-never-log"
_REF = "env:NIKA_DEADLINE_TEST_SECRET"


class _Resolver:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _REF
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return _SECRET


def _request(timeout: float) -> ModelRequest:
    return ModelRequest(
        request_id="cloud-deadline-regression",
        messages=(ModelMessage(role="user", content="Привіт"),),
        provider_id="deadline-cloud",
        model="model-a",
        timeout_seconds=timeout,
    )


def _credential_provider(
    resolver: _Resolver, factory: Any
) -> CredentialRefOpenAICompatibleProvider:
    return CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="deadline-cloud",
            base_url="https://authorized.example.test/v1",
            default_model="model-a",
            credential_ref=_REF,
        ),
        credential_resolver=resolver,
        client_factory=factory,
    )


def _http_factory(seen: list[float], effects: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        effects.append(request.url.host or "")
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Готово"},
                    }
                ],
            },
        )

    def factory(*, timeout: float) -> httpx.AsyncClient:
        seen.append(timeout)
        return httpx.AsyncClient(
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    return factory


def _assert_pretransport_timeout(exc: ModelGatewayError) -> None:
    assert exc.code is ModelErrorCode.TIMEOUT
    assert exc.provider_id == "deadline-cloud"
    assert exc.retryable is False
    assert exc.failure_effect is ModelFailureEffect.NO_EFFECT
    assert exc.__cause__ is None
    assert _SECRET not in repr(exc)
    assert _REF not in repr(exc)


def test_slow_credential_resolver_never_creates_client_or_sends_http() -> None:
    seen: list[float] = []
    effects: list[str] = []
    resolver = _Resolver(delay=0.04)
    provider = _credential_provider(resolver, _http_factory(seen, effects))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request(0.01)))

    _assert_pretransport_timeout(caught.value)
    assert resolver.calls == 1
    assert seen == []
    assert effects == []


def test_credential_lookup_is_charged_to_transmitted_http_timeout() -> None:
    seen: list[float] = []
    effects: list[str] = []
    resolver = _Resolver(delay=0.02)
    provider = _credential_provider(resolver, _http_factory(seen, effects))
    request = _request(1.0)

    response = asyncio.run(provider.complete(request))

    assert response.text == "Готово"
    assert resolver.calls == 1
    assert effects == ["authorized.example.test"]
    assert len(seen) == 1
    assert 0 < seen[0] < request.timeout_seconds - 0.01


def test_blocking_http_client_factory_cannot_start_late_network_effect() -> None:
    seen: list[float] = []
    effects: list[str] = []
    factory = _http_factory(seen, effects)

    def blocked_factory(*, timeout: float) -> httpx.AsyncClient:
        time.sleep(0.04)
        return factory(timeout=timeout)

    provider = OpenAICompatibleProvider(
        provider_id="deadline-cloud",
        base_url="https://authorized.example.test/v1",
        default_model="model-a",
        kind=ProviderKind.CLOUD,
        client_factory=blocked_factory,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request(0.01)))

    _assert_pretransport_timeout(caught.value)
    assert len(seen) == 1
    assert effects == []


def test_slow_async_client_enter_cannot_start_late_network_effect() -> None:
    events: list[str] = []

    class SlowClient:
        async def __aenter__(self) -> SlowClient:
            events.append("enter")
            await asyncio.sleep(0.04)
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("exit")

        async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            events.append("post")
            raise AssertionError("expired request must not reach HTTP post")

    provider = OpenAICompatibleProvider(
        provider_id="deadline-cloud",
        base_url="https://authorized.example.test/v1",
        default_model="model-a",
        kind=ProviderKind.CLOUD,
        client_factory=lambda **_kwargs: SlowClient(),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request(0.01)))

    _assert_pretransport_timeout(caught.value)
    assert events == ["enter", "exit"]


def test_slow_provider_constructor_cannot_reset_credential_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []
    effects: list[str] = []
    original = OpenAICompatibleProvider

    class SlowProvider(original):
        def __init__(self, **kwargs: Any) -> None:
            time.sleep(0.04)
            super().__init__(**kwargs)

    provider = _credential_provider(_Resolver(), _http_factory(seen, effects))
    monkeypatch.setattr(api_route, "OpenAICompatibleProvider", SlowProvider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request(0.01)))

    _assert_pretransport_timeout(caught.value)
    assert seen == []
    assert effects == []


class _SlowCloudAuthorizer:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def authorize_cloud_effect(
        self, *, request: ModelRequest, provider: ProviderCapabilities
    ) -> None:
        assert provider.provider_id == "deadline-cloud"
        self.calls += 1
        time.sleep(self.delay)


def test_gateway_charges_authorization_time_to_provider_http_budget() -> None:
    effects: list[str] = []
    seen: list[float] = []
    resolver = _Resolver()
    factory = _http_factory(seen, effects)

    def blocked_factory(*, timeout: float) -> httpx.AsyncClient:
        time.sleep(0.55)
        return factory(timeout=timeout)

    authorizer = _SlowCloudAuthorizer(delay=0.30)
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(_credential_provider(resolver, blocked_factory))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(0.80)))

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert authorizer.calls == 1
    assert resolver.calls == 1
    assert len(seen) == 1
    assert effects == []


def test_gateway_passes_current_post_authorization_budget_to_provider() -> None:
    authorizer = _SlowCloudAuthorizer(delay=0.03)

    class CapturingProvider:
        def __init__(self) -> None:
            self.received: list[float] = []
            self.capabilities = ProviderCapabilities(
                provider_id="deadline-cloud",
                kind=ProviderKind.CLOUD,
                supports_private_data=False,
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.received.append(request.timeout_seconds)
            return ModelResponse(
                request_id=request.request_id,
                text="Готово",
                provider_id="deadline-cloud",
                provider_kind=ProviderKind.CLOUD,
                model="model-a",
            )

    provider = CapturingProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)
    request = _request(1.0)

    response = asyncio.run(gateway.complete(request))

    assert response.text == "Готово"
    assert len(provider.received) == 1
    assert 0 < provider.received[0] < request.timeout_seconds - 0.01
    assert request.timeout_seconds == 1.0
