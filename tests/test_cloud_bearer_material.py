from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
    EnvironmentCredentialResolver,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)
from nika_core.model_gateway.gateway import ModelGateway

_REF = "env:NIKA_BEARER_TEST_TOKEN"


class _Resolver:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _REF
        self.calls += 1
        return self.value


def _provider(
    resolver: Any, client_factory: Any
) -> CredentialRefOpenAICompatibleProvider:
    return CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="test-cloud-bearer",
            base_url="https://approved.example.test/v1",
            default_model="model-a",
            credential_ref=_REF,
        ),
        credential_resolver=resolver,
        client_factory=client_factory,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="cloud-bearer-test",
        messages=(ModelMessage(role="user", content="Привіт"),),
        model="model-a",
        provider_id="test-cloud-bearer",
        timeout_seconds=2.0,
    )


@pytest.mark.parametrize(
    "material",
    (
        "token\nnewline",
        "token\rcarriage",
        "token\tseparator",
        "token\x7fdelete",
        "token\x1fcontrol",
        "token with spaces",
        " token",
        "token-π",
        "token\u200eformat",
        "token\r\nX-Injected: yes",
    ),
)
def test_invalid_bearer_material_fails_before_client_or_http(
    material: str,
) -> None:
    resolver = _Resolver(material)
    client_calls: list[float] = []

    def forbidden_client(*, timeout: float) -> httpx.AsyncClient:
        client_calls.append(timeout)
        raise AssertionError("invalid credential must never create a client")

    provider = _provider(resolver, forbidden_client)
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.provider_id == "test-cloud-bearer"
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert error.retryable is False
    assert error.__cause__ is None
    assert material not in repr(error)
    assert resolver.calls == 1
    assert client_calls == []


def test_environment_credential_is_validated_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_BEARER_TEST_TOKEN", "synthetic\r\nHeader: injected")
    client_calls: list[float] = []

    def forbidden_client(*, timeout: float) -> httpx.AsyncClient:
        client_calls.append(timeout)
        raise AssertionError("invalid environment token reached HTTP")

    provider = _provider(EnvironmentCredentialResolver(), forbidden_client)
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.AUTHENTICATION
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert client_calls == []


def test_invalid_material_produces_redacted_gateway_audit_without_fallback() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    material = "synthetic\nuntrusted-header"

    class Audit:
        def append(
            self, *, event_type: str, payload: dict[str, object], **_kwargs: Any
        ) -> int:
            events.append((event_type, payload))
            return len(events)

    class Authorizer:
        def authorize_cloud_effect(self, **_kwargs: Any) -> None:
            return None

    provider = _provider(
        _Resolver(material),
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid credential reached client")
        ),
    )
    gateway = ModelGateway(audit_log=Audit(), cloud_effect_authorizer=Authorizer())
    gateway.register(provider)
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.AUTHENTICATION
    assert material not in str(caught.value)
    assert [event for event, _ in events] == ["model.requested", "model.failed"]
    assert events[-1][1]["code"] == ModelErrorCode.AUTHENTICATION.value
    assert events[-1][1]["failure_effect"] == ModelFailureEffect.NO_EFFECT.value
    assert material not in repr(events)


def test_valid_ascii_bearer_token_reaches_only_approved_host() -> None:
    token = "synthetic-valid-Bearer_123~+/="
    seen: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
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

    provider = _provider(
        _Resolver(token),
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    response = asyncio.run(provider.complete(_request()))

    assert response.text == "Готово"
    assert seen == [("approved.example.test", f"Bearer {token}")]
