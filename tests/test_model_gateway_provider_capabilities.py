from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

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


class _MutableCapabilitiesProvider:
    def __init__(
        self,
        *,
        provider_id: str = "primary",
        supports_private_data: bool = False,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=supports_private_data,
            supports_hard_cancellation=False,
        )
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def mutate_capabilities(self, **changes: object) -> None:
        self._capabilities = replace(self._capabilities, **changes)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="provider output",
            provider_id=self._capabilities.provider_id,
            provider_kind=self._capabilities.kind,
            model=request.model or "fixture-model",
        )


def _request(*, privacy: PrivacyClass = PrivacyClass.PUBLIC) -> ModelRequest:
    return ModelRequest(
        request_id="capability-contract-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="primary",
        fallback_provider_ids=("fallback",),
        privacy=privacy,
        timeout_seconds=2.0,
    )


def test_registration_uses_declared_capabilities_without_inference_probe() -> None:
    provider = _MutableCapabilitiesProvider()
    gateway = ModelGateway()

    gateway.register(provider, default=True)

    assert gateway.providers() == ("primary",)
    assert provider.complete_calls == 0


@pytest.mark.parametrize(
    ("changes", "privacy"),
    (
        ({"supports_private_data": True}, PrivacyClass.PRIVATE),
        ({"supports_hard_cancellation": True}, PrivacyClass.PUBLIC),
        ({"kind": ProviderKind.CLOUD}, PrivacyClass.PUBLIC),
    ),
)
def test_capability_drift_fails_closed_before_any_provider_request(
    changes: dict[str, object],
    privacy: PrivacyClass,
) -> None:
    primary = _MutableCapabilitiesProvider()
    fallback = _MutableCapabilitiesProvider(
        provider_id="fallback",
        supports_private_data=True,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)
    primary.mutate_capabilities(**changes)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(privacy=privacy)))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "primary"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert primary.complete_calls == 0
    assert fallback.complete_calls == 0


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("provider_id", " primary"),
        ("kind", "local"),
        ("supports_private_data", 1),
        ("supports_tools", "yes"),
        ("supports_streaming", None),
        ("supports_hard_cancellation", 1),
    ),
)
def test_registration_rejects_malformed_nika_capability_values(
    field_name: str,
    bad_value: object,
) -> None:
    provider = _MutableCapabilitiesProvider()
    provider.mutate_capabilities(**{field_name: bad_value})
    gateway = ModelGateway()

    with pytest.raises(ValueError, match="provider capabilit"):
        gateway.register(provider)

    assert gateway.providers() == ()
    assert provider.complete_calls == 0
