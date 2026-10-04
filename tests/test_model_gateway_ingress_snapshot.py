from __future__ import annotations

import asyncio

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


class _Provider:
    def __init__(self, *, kind: ProviderKind = ProviderKind.LOCAL) -> None:
        self.kind = kind
        self.calls = 0
        self.received: ModelRequest | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.pause = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="provider-x",
            kind=self.kind,
            supports_private_data=self.kind is ProviderKind.LOCAL,
            effect_network_host=(
                "provider.example.test" if self.kind is ProviderKind.CLOUD else None
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.received = request
        self.started.set()
        if self.pause:
            await self.release.wait()
        return ModelResponse(
            request_id=request.request_id,
            provider_id="provider-x",
            provider_kind=self.kind,
            model=request.model or "model-a",
            text="вивід 🧠",
        )


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        assert entity_type == "model_request"
        self.events.append((event_type, entity_id))
        return len(self.events)


class _MutatingApproval:
    def __init__(self, field: str, value: object) -> None:
        self.field = field
        self.value = value
        self.calls = 0

    def authorize_cloud_effect(
        self, *, request: ModelRequest, provider: ProviderCapabilities
    ) -> None:
        self.calls += 1
        assert provider.provider_id == "provider-x"
        object.__setattr__(request, self.field, self.value)


def _request(*, privacy: PrivacyClass = PrivacyClass.PRIVATE) -> ModelRequest:
    return ModelRequest(
        request_id="trusted-request",
        model="model-a",
        provider_id="provider-x",
        messages=(ModelMessage(role="user", content="Привіт"),),
        metadata={"note": "початкове значення"},
        privacy=privacy,
    )


def test_gateway_uses_private_request_identity_across_provider_await() -> None:
    async def run() -> None:
        provider = _Provider()
        provider.pause = True
        audit = _Audit()
        gateway = ModelGateway(audit_log=audit)
        gateway.register(provider)
        caller_request = _request()
        operation = asyncio.create_task(gateway.complete(caller_request))
        await asyncio.wait_for(provider.started.wait(), timeout=2.0)

        object.__setattr__(caller_request, "request_id", "forged-request")
        object.__setattr__(caller_request, "model", "forged-model")
        object.__setattr__(caller_request, "metadata", {"note": "forged-metadata"})
        provider.release.set()

        response = await asyncio.wait_for(operation, timeout=2.0)
        assert response.request_id == "trusted-request"
        assert response.model == "model-a"
        assert response.text == "вивід 🧠"
        assert provider.received is not caller_request
        assert provider.received is not None
        assert provider.received.metadata["note"] == "початкове значення"
        assert audit.events == [
            ("model.requested", "trusted-request"),
            ("model.completed", "trusted-request"),
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("request_id", "bad\ud800"),
        ("messages", []),
        ("model", object()),
        ("privacy", "public"),
        ("metadata", {"note": "bad\ud800"}),
        ("timeout_seconds", "60"),
    ),
)
def test_gateway_rejects_poisoned_request_before_provider_effect(
    field: str, value: object
) -> None:
    provider = _Provider()
    gateway = ModelGateway()
    gateway.register(provider)
    request = _request()
    object.__setattr__(request, field, value)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(request))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.retryable is False
    assert provider.calls == 0


def test_gateway_rejects_non_request_carrier_before_provider_effect() -> None:
    provider = _Provider()
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(object()))  # type: ignore[arg-type]

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (("model", "retargeted-model"), ("metadata", {"note": "bad\ud800"})),
)
def test_cloud_authorizer_cannot_retarget_request_after_approval(
    field: str, value: object
) -> None:
    provider = _Provider(kind=ProviderKind.CLOUD)
    authorizer = _MutatingApproval(field, value)
    audit = _Audit()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer, audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(privacy=PrivacyClass.PUBLIC)))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert caught.value.retryable is False
    assert authorizer.calls == 1
    assert provider.calls == 0
    assert audit.events == [
        ("model.requested", "trusted-request"),
        ("model.failed", "trusted-request"),
    ]
