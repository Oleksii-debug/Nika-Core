from __future__ import annotations

import asyncio

import pytest

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


class _Provider:
    def __init__(
        self,
        *,
        provider_id: str = "provider-x",
        kind: ProviderKind = ProviderKind.LOCAL,
        model: str = "модель-🧠",
        effect_host: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.kind = kind
        self.model = model
        self.effect_host = effect_host
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            kind=self.kind,
            supports_private_data=self.kind is ProviderKind.LOCAL,
            effect_network_host=self.effect_host,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="Відповідь 🧠",
            provider_id=self.provider_id,
            provider_kind=self.kind,
            model=self.model,
        )


class _Audit:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.events.append(event_type)
        return len(self.events)


@pytest.mark.parametrize("provider_id", ("bad\ud800", "\udfff", "bad\u200b", "bad\nid"))
def test_provider_registration_rejects_nonprintable_identity(
    provider_id: str,
) -> None:
    gateway = ModelGateway()

    with pytest.raises(ValueError, match="model provider_id must be canonical text"):
        gateway.register(_Provider(provider_id=provider_id, model="model-a"))

    assert gateway.providers() == ()


@pytest.mark.parametrize(
    "host",
    ("bad host.example", "host\u200b.example", "host\ud800.example", "api.\nexample"),
)
def test_cloud_effect_host_registration_rejects_invalid_unicode_or_spacing(
    host: str,
) -> None:
    gateway = ModelGateway()

    with pytest.raises(ValueError, match="model provider effect host must be canonical"):
        gateway.register(
            _Provider(kind=ProviderKind.CLOUD, model="model-a", effect_host=host)
        )

    assert gateway.providers() == ()


@pytest.mark.parametrize("model", ("model\ud800", "\udfff", "model\u200b", "model\nid"))
def test_unpinned_provider_model_identity_cannot_publish_success(model: str) -> None:
    provider = _Provider(model=model)
    audit = _Audit()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)
    request = ModelRequest(
        request_id="unpinned-model",
        messages=(ModelMessage(role="user", content="Привіт"),),
        provider_id="provider-x",
        privacy=PrivacyClass.PRIVATE,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(request))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is False
    assert "model\ud800" not in str(caught.value)
    assert provider.calls == 1
    assert audit.events == ["model.requested", "model.failed"]


def test_valid_unicode_model_identity_survives_gateway_and_audit() -> None:
    provider = _Provider(model="модель-🧠")
    audit = _Audit()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)
    request = ModelRequest(
        request_id="valid-model",
        messages=(ModelMessage(role="user", content="Привіт 🧠"),),
        provider_id="provider-x",
        privacy=PrivacyClass.PRIVATE,
    )

    response = asyncio.run(gateway.complete(request))

    assert response.model == "модель-🧠"
    assert response.text == "Відповідь 🧠"
    assert provider.calls == 1
    assert audit.events == ["model.requested", "model.completed"]


def test_valid_unicode_cloud_host_preserves_registration_without_effect() -> None:
    gateway = ModelGateway()
    provider = _Provider(
        kind=ProviderKind.CLOUD,
        model="model-a",
        effect_host="api.приклад.укр",
    )

    gateway.register(provider)

    assert gateway.providers() == ("provider-x",)
    assert provider.calls == 0
