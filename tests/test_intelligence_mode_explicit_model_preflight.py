from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.modes import (
    IntelligenceMode,
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
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _DefaultingProvider:
    """Regression oracle: this provider would silently default if it were called."""

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
        selected = request.model if isinstance(request.model, str) and request.model else "default"
        return ModelResponse(
            request_id=request.request_id,
            text="must not be reached for an invalid explicit model",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=selected,
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


def _request(model: object) -> ModelRequest:
    return ModelRequest(
        request_id="explicit-model-preflight",
        messages=(ModelMessage(role="user", content="task"),),
        model=model,  # type: ignore[arg-type]
        provider_id="incoming-provider",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("incoming-fallback",),
    )


@pytest.mark.parametrize(
    "mode",
    (
        IntelligenceMode.EMBEDDED_LOCAL,
        IntelligenceMode.EXTERNAL_LOCAL,
        IntelligenceMode.EXTERNAL_API,
    ),
)
@pytest.mark.parametrize("model", ("", "   ", " model-a", "model-a ", "model\x00a", 0))
def test_invalid_explicit_model_never_reaches_a_defaulting_provider(
    mode: IntelligenceMode,
    model: object,
) -> None:
    provider_id, kind, policy = _route(mode)
    gateway = ModelGateway()
    provider = _DefaultingProvider(provider_id=provider_id, kind=kind)
    gateway.register(provider, default=True)
    router = IntelligenceModeRouter(gateway=gateway, policy=policy)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(mode, _request(model)))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.provider_id == provider_id
    assert caught.value.retryable is False
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.calls == 0
