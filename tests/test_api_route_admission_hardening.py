from __future__ import annotations

import asyncio

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
    CredentialResolutionError,
    EnvironmentCredentialResolver,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)

_REF = "env:NIKA_ROUTE_ADMISSION_TEST_TOKEN"


def _config(base_url: str = "https://api.example.test/v1") -> ApiModelRouteConfig:
    return ApiModelRouteConfig(
        provider_id="approved-api",
        base_url=base_url,
        default_model="model-a",
        credential_ref=_REF,
    )


class _Resolver:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.calls = 0

    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _REF
        self.calls += 1
        return self.secret


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="api-route-admission",
        provider_id="approved-api",
        messages=(ModelMessage(role="user", content="safe fixture"),),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.test/v1\t/chat",
        "https://api.example.test/v1\n/chat",
        "https://api.example.test/v1\r/chat",
        "https://api.example.test/v1\\other",
        "https://api.example.test/v1\u200b",
        "https://api.example.test:65536/v1",
        "https://api.example.test:abc/v1",
        "https://api.example.test:0/v1",
        "https://api.example.test:/v1",
        "https://[::1/v1",
        "https://api.example%2Etest/v1",
        "https://api.example.test/v1?",
        "https://api.example.test/v1#",
    ],
)
def test_ambiguous_route_is_rejected_before_provider_construction(
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        _config(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.test/v1",
        "https://api.example.test:443/v1",
        "https://[2001:db8::1]:443/v1",
        "https://api.example.test/версія",
    ],
)
def test_explicit_valid_https_route_remains_supported(base_url: str) -> None:
    assert _config(base_url).base_url == base_url


@pytest.mark.parametrize(
    "secret",
    [
        "valid\r\nX-Injection: yes",
        "bad\n",
        "\x00",
        "space in key",
        " leading-key",
        "trailing-key ",
        "nonascii-é",
        "surrogate-\ud800",
        "x" * 8193,
    ],
)
def test_unsafe_bearer_is_denied_before_http_client_creation(secret: str) -> None:
    resolver = _Resolver(secret)
    creations = 0

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        nonlocal creations
        creations += 1
        raise AssertionError(f"network factory must not be called: {timeout}")

    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=resolver,
        client_factory=client_factory,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert error.__cause__ is None
    assert error.__context__ is None
    assert resolver.calls == 1
    assert creations == 0


def test_longest_allowed_printable_ascii_credential_remains_valid() -> None:
    token = "a" * 8192
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_Resolver(token),
        client_factory=client_factory,
    )
    response = asyncio.run(provider.complete(_request()))

    assert response.text == "ok"
    assert seen == [f"Bearer {token}"]


def test_environment_resolver_rejects_control_bearing_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = EnvironmentCredentialResolver()
    monkeypatch.setattr(
        "nika_core.model_gateway.api_route.os.environ",
        {
            "NIKA_ROUTE_ADMISSION_TEST_TOKEN": "safe",
            "NIKA_ROUTE_ADMISSION_TEST_TOKEN\nINJECTED": "safe",
        },
    )

    assert resolver.resolve(_REF) == "safe"
    with pytest.raises(CredentialResolutionError, match="invalid"):
        resolver.resolve("env:NIKA_ROUTE_ADMISSION_TEST_TOKEN\nINJECTED")


def test_provider_rejects_route_config_subclasses_before_attribute_access() -> None:
    class DerivedConfig(ApiModelRouteConfig):
        def __getattribute__(self, name: str) -> object:
            if name == "provider_id":
                raise AssertionError("config subclass behavior must not execute")
            return super().__getattribute__(name)

    config = object.__new__(DerivedConfig)

    with pytest.raises(TypeError, match="config must be an ApiModelRouteConfig"):
        CredentialRefOpenAICompatibleProvider(
            config=config,
            credential_resolver=_Resolver("stable-token"),
        )


def test_resolver_exception_does_not_survive_as_exception_context() -> None:
    canary = "SYNTHETIC_RESOLVER_SECRET_CANARY"

    class LeakingResolver:
        def resolve(self, credential_ref: str) -> str:
            raise RuntimeError(f"{credential_ref}: {canary}")

    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=LeakingResolver(),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in repr(error)


def test_http_error_does_not_survive_as_exception_context() -> None:
    canary = "SYNTHETIC_HTTP_HEADER_SECRET_CANARY"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {canary}"
        return httpx.Response(401, json={"error": "unauthorized"})

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_Resolver(canary),
        client_factory=client_factory,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in repr(error)


def test_untyped_transport_exception_is_sanitized_without_context() -> None:
    canary = "SYNTHETIC_TRANSPORT_SECRET_CANARY"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {canary}"
        raise RuntimeError(f"unsafe transport diagnostic: {canary}")

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_Resolver(canary),
        client_factory=client_factory,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert error.retryable is False
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in repr(error)
