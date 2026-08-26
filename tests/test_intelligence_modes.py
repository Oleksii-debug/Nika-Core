from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModeError,
    IntelligenceModeErrorCode,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class RecordingGateway(ModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


class RecordingProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        response_kind: ProviderKind | None = None,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self._response_kind = response_kind or kind
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self._response_kind,
            model=request.model or "test-model",
        )


class RecordingDeterministicCompletion:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text="deterministic result",
            provider_id="nika-deterministic",
            provider_kind=ProviderKind.NO_LLM,
            model="deterministic",
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="private task"),),
        provider_id="untrusted-route",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("untrusted-fallback",),
    )


def test_deterministic_mode_never_invokes_model_gateway() -> None:
    gateway = RecordingGateway()
    unexpected_provider = RecordingProvider(provider_id="untrusted-route", kind=ProviderKind.CLOUD)
    gateway.register(unexpected_provider, default=True)
    deterministic = RecordingDeterministicCompletion()
    router = IntelligenceModeRouter(gateway=gateway, deterministic=deterministic)

    response = asyncio.run(router.complete(IntelligenceMode.DETERMINISTIC, _request()))

    assert response.provider_kind is ProviderKind.NO_LLM
    assert gateway.requests == []
    assert unexpected_provider.requests == []
    assert len(deterministic.requests) == 1
    routed = deterministic.requests[0]
    assert routed.provider_id is None
    assert routed.provider_kind is ProviderKind.NO_LLM
    assert routed.fallback_provider_ids == ()


def test_embedded_local_mode_pins_foundry_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    foundry = RecordingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    gateway.register(foundry)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
    )

    response = asyncio.run(router.complete(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    assert response.provider_id == "foundry-local"
    assert len(gateway.requests) == 1
    routed = gateway.requests[0]
    assert routed.provider_id == "foundry-local"
    assert routed.provider_kind is None
    assert routed.fallback_provider_ids == ()


def test_local_ollama_mode_pins_ollama_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    ollama = RecordingProvider(provider_id="ollama", kind=ProviderKind.LOCAL)
    gateway.register(ollama)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
    )

    response = asyncio.run(router.complete(IntelligenceMode.LOCAL_OLLAMA, _request()))

    assert response.provider_id == "ollama"
    routed = gateway.requests[0]
    assert routed.provider_id == "ollama"
    assert routed.provider_kind is None
    assert routed.fallback_provider_ids == ()


def test_external_api_mode_is_disabled_by_default() -> None:
    gateway = RecordingGateway()
    cloud = RecordingProvider(provider_id="approved-cloud", kind=ProviderKind.CLOUD)
    gateway.register(cloud, default=True)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.MODE_DISABLED
    assert caught.value.mode is IntelligenceMode.EXTERNAL_API
    assert gateway.requests == []
    assert cloud.requests == []


def test_approved_external_api_mode_routes_only_to_cloud_default() -> None:
    gateway = RecordingGateway()
    cloud = RecordingProvider(provider_id="approved-cloud", kind=ProviderKind.CLOUD)
    gateway.register(cloud, default=True)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
        policy=IntelligenceModePolicy(external_api_enabled=True),
    )

    response = asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert response.provider_id == "approved-cloud"
    routed = gateway.requests[0]
    assert routed.provider_id is None
    assert routed.provider_kind is ProviderKind.CLOUD
    assert routed.fallback_provider_ids == ()


def test_disabled_local_mode_fails_before_gateway_invocation() -> None:
    gateway = RecordingGateway()
    foundry = RecordingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    gateway.register(foundry)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
        policy=IntelligenceModePolicy(embedded_local_enabled=False),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.MODE_DISABLED
    assert gateway.requests == []
    assert foundry.requests == []


def test_provider_kind_mismatch_fails_closed() -> None:
    gateway = RecordingGateway()
    compromised = RecordingProvider(
        provider_id="foundry-local",
        kind=ProviderKind.LOCAL,
        response_kind=ProviderKind.CLOUD,
    )
    gateway.register(compromised)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministicCompletion(),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH
    assert caught.value.mode is IntelligenceMode.EMBEDDED_LOCAL


def test_mode_statuses_are_secret_free_and_external_is_opt_in() -> None:
    router = IntelligenceModeRouter(
        gateway=RecordingGateway(),
        deterministic=RecordingDeterministicCompletion(),
    )

    statuses = router.statuses()

    assert tuple(status.mode for status in statuses) == tuple(IntelligenceMode)
    external = statuses[-1]
    assert external.mode is IntelligenceMode.EXTERNAL_API
    assert external.enabled is False
    assert external.provider_id is None
    assert external.provider_kind is ProviderKind.CLOUD
    assert "private task" not in repr(statuses)
    assert "untrusted-route" not in repr(statuses)


def test_mode_policy_rejects_ambiguous_or_whitespace_provider_ids() -> None:
    with pytest.raises(ValueError, match="distinct"):
        IntelligenceModePolicy(
            embedded_provider_id="local",
            ollama_provider_id="local",
        )

    with pytest.raises(ValueError, match="surrounding whitespace"):
        IntelligenceModePolicy(ollama_provider_id=" ollama")
