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
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
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
        supports_private_data: bool = True,
        response_provider_id: str | None = None,
        response_kind: ProviderKind | None = None,
        response_request_id: str | None = None,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
        )
        self._response_provider_id = response_provider_id
        self._response_kind = response_kind
        self._response_request_id = response_request_id
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=self._response_request_id or request.request_id,
            text="ok",
            provider_id=self._response_provider_id or self.capabilities.provider_id,
            provider_kind=self._response_kind or self.capabilities.kind,
            model=request.model or "test-model",
        )


class RecordingDeterministic:
    def __init__(
        self,
        *,
        response_kind: ProviderKind = ProviderKind.NO_LLM,
        response_request_id: str | None = None,
    ) -> None:
        self._response_kind = response_kind
        self._response_request_id = response_request_id
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=self._response_request_id or request.request_id,
            text="deterministic result",
            provider_id="nika-deterministic",
            provider_kind=self._response_kind,
            model="deterministic",
        )


def _request(*, privacy: PrivacyClass = PrivacyClass.PRIVATE) -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="private task"),),
        provider_id="untrusted-route",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("untrusted-fallback",),
        privacy=privacy,
        timeout_seconds=12.0,
    )


def test_deterministic_mode_bypasses_gateway_and_strips_model_routes() -> None:
    gateway = RecordingGateway()
    unexpected = RecordingProvider(provider_id="untrusted-route", kind=ProviderKind.CLOUD)
    gateway.register(unexpected, default=True)
    deterministic = RecordingDeterministic()
    router = IntelligenceModeRouter(gateway=gateway, deterministic=deterministic)
    original = _request()

    response = asyncio.run(router.complete(IntelligenceMode.DETERMINISTIC, original))

    assert response.provider_kind is ProviderKind.NO_LLM
    assert gateway.requests == []
    assert unexpected.requests == []
    assert len(deterministic.requests) == 1
    routed = deterministic.requests[0]
    assert routed.provider_id is None
    assert routed.provider_kind is ProviderKind.NO_LLM
    assert routed.fallback_provider_ids == ()
    assert original.provider_id == "untrusted-route"
    assert original.fallback_provider_ids == ("untrusted-fallback",)


def test_embedded_local_mode_pins_foundry_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    foundry = RecordingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    other = RecordingProvider(provider_id="untrusted-fallback", kind=ProviderKind.CLOUD)
    gateway.register(foundry)
    gateway.register(other)
    router = IntelligenceModeRouter(gateway=gateway, deterministic=RecordingDeterministic())

    response = asyncio.run(router.complete(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    assert response.provider_id == "foundry-local"
    assert len(foundry.requests) == 1
    assert other.requests == []
    routed = gateway.requests[0]
    assert routed.provider_id == "foundry-local"
    assert routed.provider_kind is None
    assert routed.fallback_provider_ids == ()


def test_local_ollama_mode_pins_ollama_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    ollama = RecordingProvider(provider_id="ollama", kind=ProviderKind.LOCAL)
    other = RecordingProvider(provider_id="untrusted-fallback", kind=ProviderKind.CLOUD)
    gateway.register(ollama)
    gateway.register(other)
    router = IntelligenceModeRouter(gateway=gateway, deterministic=RecordingDeterministic())

    response = asyncio.run(router.complete(IntelligenceMode.LOCAL_OLLAMA, _request()))

    assert response.provider_id == "ollama"
    assert len(ollama.requests) == 1
    assert other.requests == []
    assert gateway.requests[0].fallback_provider_ids == ()


def test_external_api_mode_is_disabled_by_default() -> None:
    gateway = RecordingGateway()
    cloud = RecordingProvider(provider_id="approved-cloud", kind=ProviderKind.CLOUD)
    gateway.register(cloud, default=True)
    router = IntelligenceModeRouter(gateway=gateway, deterministic=RecordingDeterministic())

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.MODE_DISABLED
    assert gateway.requests == []
    assert cloud.requests == []


def test_approved_external_mode_pins_exact_provider_not_gateway_default() -> None:
    gateway = RecordingGateway()
    approved = RecordingProvider(provider_id="approved-cloud", kind=ProviderKind.CLOUD)
    other_default = RecordingProvider(provider_id="other-cloud", kind=ProviderKind.CLOUD)
    gateway.register(approved)
    gateway.register(other_default, default=True)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministic(),
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    response = asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert response.provider_id == "approved-cloud"
    assert len(approved.requests) == 1
    assert other_default.requests == []
    assert gateway.requests[0].provider_id == "approved-cloud"
    assert gateway.requests[0].fallback_provider_ids == ()


def test_external_private_route_is_blocked_before_provider_call() -> None:
    gateway = RecordingGateway()
    cloud = RecordingProvider(
        provider_id="approved-cloud",
        kind=ProviderKind.CLOUD,
        supports_private_data=False,
    )
    gateway.register(cloud)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministic(),
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert len(gateway.requests) == 1
    assert cloud.requests == []


@pytest.mark.parametrize(
    ("mode", "policy"),
    (
        (
            IntelligenceMode.EMBEDDED_LOCAL,
            IntelligenceModePolicy(embedded_local_enabled=False),
        ),
        (
            IntelligenceMode.LOCAL_OLLAMA,
            IntelligenceModePolicy(local_ollama_enabled=False),
        ),
    ),
)
def test_disabled_local_modes_fail_before_gateway(
    mode: IntelligenceMode,
    policy: IntelligenceModePolicy,
) -> None:
    gateway = RecordingGateway()
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministic(),
        policy=policy,
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(mode, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.MODE_DISABLED
    assert gateway.requests == []


def test_provider_kind_substitution_fails_closed() -> None:
    gateway = RecordingGateway()
    compromised = RecordingProvider(
        provider_id="foundry-local",
        kind=ProviderKind.LOCAL,
        response_kind=ProviderKind.CLOUD,
    )
    gateway.register(compromised)
    router = IntelligenceModeRouter(gateway=gateway, deterministic=RecordingDeterministic())

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH


def test_provider_identity_substitution_fails_closed() -> None:
    gateway = RecordingGateway()
    compromised = RecordingProvider(
        provider_id="approved-cloud",
        kind=ProviderKind.CLOUD,
        response_provider_id="other-cloud",
    )
    gateway.register(compromised)
    router = IntelligenceModeRouter(
        gateway=gateway,
        deterministic=RecordingDeterministic(),
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH


def test_response_request_identity_substitution_fails_closed() -> None:
    gateway = RecordingGateway()
    ollama = RecordingProvider(
        provider_id="ollama",
        kind=ProviderKind.LOCAL,
        response_request_id="other-request",
    )
    gateway.register(ollama)
    router = IntelligenceModeRouter(gateway=gateway, deterministic=RecordingDeterministic())

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.LOCAL_OLLAMA, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH


def test_deterministic_response_cannot_claim_model_provider_kind() -> None:
    router = IntelligenceModeRouter(
        gateway=RecordingGateway(),
        deterministic=RecordingDeterministic(response_kind=ProviderKind.LOCAL),
    )

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete(IntelligenceMode.DETERMINISTIC, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.RESPONSE_MISMATCH


def test_statuses_are_secret_free_and_external_is_opt_in() -> None:
    router = IntelligenceModeRouter(
        gateway=RecordingGateway(),
        deterministic=RecordingDeterministic(),
    )

    statuses = router.statuses()

    assert tuple(status.mode for status in statuses) == tuple(IntelligenceMode)
    external = statuses[-1]
    assert external.enabled is False
    assert external.provider_id is None
    rendered = repr(statuses)
    assert "private task" not in rendered
    assert "untrusted-route" not in rendered
    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "env:" not in rendered


@pytest.mark.parametrize(
    "kwargs",
    (
        {"embedded_provider_id": "local", "ollama_provider_id": "local"},
        {"ollama_provider_id": " ollama"},
        {"external_provider_id": "ollama"},
        {"embedded_provider_id": "bad\nprovider"},
    ),
)
def test_policy_rejects_ambiguous_provider_identity(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        IntelligenceModePolicy(**kwargs)  # type: ignore[arg-type]


def test_external_enable_requires_provider_and_boolean_flags_are_strict() -> None:
    with pytest.raises(ValueError, match="external_provider_id"):
        IntelligenceModePolicy(external_api_enabled=True)
    with pytest.raises(TypeError, match="external_api_enabled"):
        IntelligenceModePolicy(external_api_enabled=1)  # type: ignore[arg-type]


def test_invalid_mode_type_fails_before_any_execution() -> None:
    gateway = RecordingGateway()
    deterministic = RecordingDeterministic()
    router = IntelligenceModeRouter(gateway=gateway, deterministic=deterministic)

    with pytest.raises(TypeError, match="IntelligenceMode"):
        asyncio.run(router.complete("local_ollama", _request()))  # type: ignore[arg-type]

    assert gateway.requests == []
    assert deterministic.requests == []
