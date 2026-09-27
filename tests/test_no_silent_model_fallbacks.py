from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModeError,
    IntelligenceModeErrorCode,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
)
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


class _RecordingGateway(ModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


class _FailingProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        code: ModelErrorCode,
        retryable: bool,
        failure_effect: ModelFailureEffect,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
            supports_hard_cancellation=True,
        )
        self._code = code
        self._retryable = retryable
        self._failure_effect = failure_effect
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise ModelGatewayError(
            self._code,
            "fixture provider failure",
            provider_id=self.capabilities.provider_id,
            retryable=self._retryable,
            failure_effect=self._failure_effect,
        )


class _AlternateProvider:
    def __init__(self, *, provider_id: str, kind: ProviderKind) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="alternate route must never execute",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "alternate-default",
        )


class _DefaultingProvider:
    def __init__(self, *, provider_id: str, kind: ProviderKind) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self.calls = 0
        self.models: list[str | None] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.models.append(request.model)
        selected_model = request.model or "provider-default"
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=selected_model,
        )


class _Resolver:
    def __init__(self, *, material: str = "fixture-secret", fail: bool = False) -> None:
        self.material = material
        self.fail = fail
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        if self.fail:
            raise RuntimeError("fixture resolver failure")
        return self.material


def _request(*, model: object = "pinned-model") -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="private task"),),
        model=model,  # type: ignore[arg-type]
        provider_id="incoming-provider",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("incoming-fallback-a", "incoming-fallback-b"),
        timeout_seconds=5.0,
    )


def _route(mode: IntelligenceMode) -> tuple[str, ProviderKind, IntelligenceModePolicy]:
    if mode is IntelligenceMode.EMBEDDED_LOCAL:
        return "foundry-local", ProviderKind.LOCAL, IntelligenceModePolicy()
    if mode is IntelligenceMode.EXTERNAL_LOCAL:
        return "ollama", ProviderKind.LOCAL, IntelligenceModePolicy()
    if mode is IntelligenceMode.EXTERNAL_API:
        return (
            "cloud-a",
            ProviderKind.CLOUD,
            IntelligenceModePolicy(
                external_api_enabled=True,
                external_provider_id="cloud-a",
            ),
        )
    raise AssertionError("model-backed mode required")


_FAILURES = (
    (ModelErrorCode.UNAVAILABLE, True, ModelFailureEffect.NO_EFFECT),
    (ModelErrorCode.UNAVAILABLE, False, ModelFailureEffect.NO_EFFECT),
    (ModelErrorCode.TIMEOUT, True, ModelFailureEffect.NO_EFFECT),
    (ModelErrorCode.PROVIDER_ERROR, False, ModelFailureEffect.UNKNOWN),
    (ModelErrorCode.AUTHENTICATION, False, ModelFailureEffect.NO_EFFECT),
    (ModelErrorCode.RESOURCE_LIMIT, False, ModelFailureEffect.NO_EFFECT),
    (ModelErrorCode.UNAVAILABLE, False, ModelFailureEffect.NO_EFFECT),
)


@pytest.mark.parametrize(
    "mode",
    (
        IntelligenceMode.EMBEDDED_LOCAL,
        IntelligenceMode.EXTERNAL_LOCAL,
        IntelligenceMode.EXTERNAL_API,
    ),
)
@pytest.mark.parametrize(("code", "retryable", "failure_effect"), _FAILURES)
def test_selected_route_never_crosses_provider_on_failure(
    mode: IntelligenceMode,
    code: ModelErrorCode,
    retryable: bool,
    failure_effect: ModelFailureEffect,
) -> None:
    provider_id, kind, policy = _route(mode)
    gateway = _RecordingGateway()
    selected = _FailingProvider(
        provider_id=provider_id,
        kind=kind,
        code=code,
        retryable=retryable,
        failure_effect=failure_effect,
    )
    gateway.register(selected)

    alternates = (
        _AlternateProvider(provider_id="fallback-local", kind=ProviderKind.LOCAL),
        _AlternateProvider(provider_id="fallback-cloud", kind=ProviderKind.CLOUD),
        _AlternateProvider(provider_id="fallback-no-llm", kind=ProviderKind.NO_LLM),
    )
    for alternate in alternates:
        gateway.register(alternate)

    router = IntelligenceModeRouter(gateway=gateway, policy=policy)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(mode, _request()))

    assert caught.value.code is code
    assert caught.value.provider_id == provider_id
    assert caught.value.retryable is retryable
    assert caught.value.failure_effect is failure_effect
    assert selected.calls == 1
    assert [alternate.calls for alternate in alternates] == [0, 0, 0]
    assert len(gateway.requests) == 1
    assert gateway.requests[0].provider_id == provider_id
    assert gateway.requests[0].provider_kind is kind
    assert gateway.requests[0].model == "pinned-model"
    assert gateway.requests[0].fallback_provider_ids == ()


def test_model_completion_never_silently_becomes_deterministic_mode() -> None:
    gateway = _RecordingGateway()
    unexpected = _AlternateProvider(
        provider_id="fallback-cloud",
        kind=ProviderKind.CLOUD,
    )
    gateway.register(unexpected)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.DETERMINISTIC, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.DETERMINISTIC_PATH_REQUIRED
    assert gateway.requests == []
    assert unexpected.calls == 0


@pytest.mark.parametrize(
    "model",
    ("", "   ", " model-a", "model-a ", "model\x00a", "model\u200ba", 0),
)
def test_invalid_explicit_model_fails_at_canonical_request_boundary(
    model: object,
) -> None:
    gateway = ModelGateway()
    provider = _DefaultingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    gateway.register(provider, default=True)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises((TypeError, ValueError)):
        request = _request(model=model)
        asyncio.run(router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, request))

    assert provider.calls == 0
    assert provider.models == []


@pytest.mark.parametrize(
    "mode",
    (
        IntelligenceMode.EMBEDDED_LOCAL,
        IntelligenceMode.EXTERNAL_LOCAL,
        IntelligenceMode.EXTERNAL_API,
    ),
)
def test_valid_explicit_model_stays_pinned_through_selected_route(
    mode: IntelligenceMode,
) -> None:
    provider_id, kind, policy = _route(mode)
    gateway = ModelGateway()
    provider = _DefaultingProvider(provider_id=provider_id, kind=kind)
    gateway.register(provider, default=True)
    router = IntelligenceModeRouter(gateway=gateway, policy=policy)

    response = asyncio.run(router.complete_model(mode, _request(model="pinned-model")))

    assert provider.calls == 1
    assert provider.models == ["pinned-model"]
    assert response.provider_id == provider_id
    assert response.provider_kind is kind
    assert response.model == "pinned-model"


def _api_provider(
    *,
    resolver: _Resolver,
    client_factory,
) -> CredentialRefOpenAICompatibleProvider:
    return CredentialRefOpenAICompatibleProvider(
        config=ApiModelRouteConfig(
            provider_id="configured-api",
            base_url="https://api.example.test/v1",
            default_model="configured-default",
            credential_ref="env:NIKA_TEST_REFERENCE",
            supports_private_data=True,
        ),
        credential_resolver=resolver,
        client_factory=client_factory,
    )


@pytest.mark.parametrize(
    ("model", "expected_model"),
    ((None, "configured-default"), ("pinned-model", "pinned-model")),
)
def test_only_none_deliberately_selects_configured_api_default(
    model: str | None,
    expected_model: str,
) -> None:
    resolver = _Resolver()
    models_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        models_seen.append(str(payload["model"]))
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = _api_provider(resolver=resolver, client_factory=client_factory)
    response = asyncio.run(provider.complete(_request(model=model)))

    assert models_seen == [expected_model]
    assert response.model == expected_model
    assert resolver.references == ["env:NIKA_TEST_REFERENCE"]


def test_configured_api_authentication_failure_is_transport_free() -> None:
    resolver = _Resolver(fail=True)
    client_calls = 0

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        nonlocal client_calls
        del kwargs
        client_calls += 1
        raise AssertionError("credential failure must happen before transport")

    provider = _api_provider(resolver=resolver, client_factory=client_factory)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.AUTHENTICATION
    assert caught.value.provider_id == "configured-api"
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert client_calls == 0


def test_configured_api_malformed_response_stays_provider_error() -> None:
    resolver = _Resolver()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = _api_provider(resolver=resolver, client_factory=client_factory)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "configured-api"
    assert caught.value.retryable is False
    assert calls == 1
