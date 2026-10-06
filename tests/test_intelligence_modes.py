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
    ModelFailureEffect,
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


class FailingProvider:
    def __init__(self, *, provider_id: str, kind: ProviderKind) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
            supports_hard_cancellation=True,
        )
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "fixture unavailable",
            provider_id=self.capabilities.provider_id,
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
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


def test_deterministic_mode_resolves_to_real_non_gateway_boundary() -> None:
    gateway = RecordingGateway()
    router = IntelligenceModeRouter(gateway=gateway)

    route = router.resolve(IntelligenceMode.DETERMINISTIC)

    assert route.mode is IntelligenceMode.DETERMINISTIC
    assert route.provider_id is None
    assert route.provider_kind is ProviderKind.NO_LLM
    assert route.uses_model_gateway is False
    assert route.enabled is True
    assert gateway.requests == []


def test_deterministic_mode_cannot_be_used_as_model_completion() -> None:
    gateway = RecordingGateway()
    unexpected = RecordingProvider(provider_id="untrusted-route", kind=ProviderKind.CLOUD)
    gateway.register(unexpected, default=True)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.DETERMINISTIC, _request()))

    assert caught.value.code is IntelligenceModeErrorCode.DETERMINISTIC_PATH_REQUIRED
    assert gateway.requests == []
    assert unexpected.requests == []


def test_embedded_local_mode_pins_foundry_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    foundry = RecordingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    other = RecordingProvider(provider_id="untrusted-fallback", kind=ProviderKind.CLOUD)
    gateway.register(foundry)
    gateway.register(other)
    router = IntelligenceModeRouter(gateway=gateway)

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, _request())
    )

    assert response.provider_id == "foundry-local"
    assert len(foundry.requests) == 1
    assert other.requests == []
    routed = gateway.requests[0]
    assert routed.provider_id == "foundry-local"
    assert routed.provider_kind is ProviderKind.LOCAL
    assert routed.fallback_provider_ids == ()


def test_external_local_mode_defaults_to_ollama_and_strips_fallbacks() -> None:
    gateway = RecordingGateway()
    ollama = RecordingProvider(provider_id="ollama", kind=ProviderKind.LOCAL)
    other = RecordingProvider(provider_id="untrusted-fallback", kind=ProviderKind.CLOUD)
    gateway.register(ollama)
    gateway.register(other)
    router = IntelligenceModeRouter(gateway=gateway)

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, _request())
    )

    assert response.provider_id == "ollama"
    assert len(ollama.requests) == 1
    assert other.requests == []
    assert gateway.requests[0].fallback_provider_ids == ()


def test_external_local_mode_can_pin_another_registered_local_provider() -> None:
    gateway = RecordingGateway()
    local_api = RecordingProvider(provider_id="local-openai", kind=ProviderKind.LOCAL)
    ollama = RecordingProvider(provider_id="ollama", kind=ProviderKind.LOCAL)
    gateway.register(local_api)
    gateway.register(ollama)
    router = IntelligenceModeRouter(
        gateway=gateway,
        policy=IntelligenceModePolicy(external_local_provider_id="local-openai"),
    )

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, _request())
    )

    assert response.provider_id == "local-openai"
    assert len(local_api.requests) == 1
    assert ollama.requests == []


def test_selected_mode_never_uses_incoming_fallback_after_safe_provider_failure() -> None:
    gateway = RecordingGateway()
    foundry = FailingProvider(provider_id="foundry-local", kind=ProviderKind.LOCAL)
    fallback = RecordingProvider(provider_id="untrusted-fallback", kind=ProviderKind.CLOUD)
    gateway.register(foundry)
    gateway.register(fallback)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, _request())
        )

    assert caught.value.code is ModelErrorCode.UNAVAILABLE
    assert len(foundry.requests) == 1
    assert fallback.requests == []
    assert gateway.requests[0].fallback_provider_ids == ()


def test_external_api_mode_is_disabled_by_default() -> None:
    gateway = RecordingGateway()
    cloud = RecordingProvider(provider_id="approved-cloud", kind=ProviderKind.CLOUD)
    gateway.register(cloud, default=True)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))

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
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EXTERNAL_API, _request())
    )

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
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))

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
            IntelligenceMode.EXTERNAL_LOCAL,
            IntelligenceModePolicy(external_local_enabled=False),
        ),
    ),
)
def test_disabled_local_modes_fail_before_gateway(
    mode: IntelligenceMode,
    policy: IntelligenceModePolicy,
) -> None:
    gateway = RecordingGateway()
    router = IntelligenceModeRouter(gateway=gateway, policy=policy)

    with pytest.raises(IntelligenceModeError) as caught:
        asyncio.run(router.complete_model(mode, _request()))

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
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, _request())
        )

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "foundry-local"
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN


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
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "approved-cloud"
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN


def test_response_request_identity_substitution_fails_closed() -> None:
    gateway = RecordingGateway()
    ollama = RecordingProvider(
        provider_id="ollama",
        kind=ProviderKind.LOCAL,
        response_request_id="other-request",
    )
    gateway.register(ollama)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, _request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.provider_id == "ollama"
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN


def test_statuses_are_secret_free_and_external_is_opt_in() -> None:
    router = IntelligenceModeRouter(gateway=RecordingGateway())

    statuses = router.statuses()

    assert tuple(status.mode for status in statuses) == tuple(IntelligenceMode)
    deterministic = statuses[0]
    assert deterministic.uses_model_gateway is False
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
        {"embedded_provider_id": "local", "external_local_provider_id": "local"},
        {"external_local_provider_id": " ollama"},
        {"external_provider_id": "ollama"},
        {"embedded_provider_id": "bad\nprovider"},
        {"external_provider_id": "https://api.example.test"},
        {"external_provider_id": "env:NIKA_PROVIDER_REFERENCE"},
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
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(TypeError, match="IntelligenceMode"):
        router.resolve("external_local")  # type: ignore[arg-type]

    assert gateway.requests == []


def test_invalid_request_type_fails_before_gateway() -> None:
    gateway = RecordingGateway()
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(TypeError, match="ModelRequest"):
        asyncio.run(
            router.complete_model(
                IntelligenceMode.EXTERNAL_LOCAL,
                "request",  # type: ignore[arg-type]
            )
        )

    assert gateway.requests == []


@pytest.mark.parametrize(
    ("mode", "policy", "registered_kind"),
    (
        (
            IntelligenceMode.EMBEDDED_LOCAL,
            IntelligenceModePolicy(),
            ProviderKind.CLOUD,
        ),
        (
            IntelligenceMode.EXTERNAL_LOCAL,
            IntelligenceModePolicy(),
            ProviderKind.CLOUD,
        ),
        (
            IntelligenceMode.EXTERNAL_API,
            IntelligenceModePolicy(
                external_api_enabled=True,
                external_provider_id="approved-cloud",
            ),
            ProviderKind.LOCAL,
        ),
    ),
)
def test_mode_kind_mismatch_fails_before_provider_execution(
    mode: IntelligenceMode,
    policy: IntelligenceModePolicy,
    registered_kind: ProviderKind,
) -> None:
    gateway = RecordingGateway()
    route = IntelligenceModeRouter(gateway=gateway, policy=policy).resolve(mode)
    assert route.provider_id is not None
    provider = RecordingProvider(
        provider_id=route.provider_id,
        kind=registered_kind,
        supports_private_data=True,
    )
    gateway.register(provider)
    router = IntelligenceModeRouter(gateway=gateway, policy=policy)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(mode, _request()))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.provider_id == route.provider_id
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.requests == []
    assert len(gateway.requests) == 1
    assert gateway.requests[0].provider_id == route.provider_id
    assert gateway.requests[0].provider_kind is route.provider_kind
