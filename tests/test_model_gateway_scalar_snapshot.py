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
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _BehavioralStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        return str(self)

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("provider-owned string behavior crossed the trust boundary")


class _BehavioralInt(int):
    pass


class _BehavioralFloat(float):
    pass


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        assert entity_type == "model_request"
        self.events.append((event_type, dict(payload or {})))
        return len(self.events)


class _CapabilitiesScalarProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=_BehavioralStr("trusted"),
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("provider must not be registered")


class _ResponseScalarProvider:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="trusted",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = ModelResponse(
            request_id=request.request_id,
            text="provider output",
            provider_id="trusted",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
            usage=ModelUsage(input_tokens=1, output_tokens=2, total_tokens=3),
            latency_ms=1.5,
        )
        if self.mode == "model-str-subclass":
            return replace(response, model=_BehavioralStr("fixture-model"))
        if self.mode == "text-str-subclass":
            return replace(response, text=_BehavioralStr("provider output"))
        if self.mode == "provider-str-subclass":
            return replace(response, provider_id=_BehavioralStr("trusted"))
        if self.mode == "request-str-subclass":
            return replace(response, request_id=_BehavioralStr(request.request_id))
        if self.mode == "usage-int-subclass":
            return replace(
                response,
                usage=ModelUsage(
                    input_tokens=_BehavioralInt(1),
                    output_tokens=2,
                    total_tokens=3,
                ),
            )
        if self.mode == "latency-float-subclass":
            return replace(response, latency_ms=_BehavioralFloat(1.5))
        raise AssertionError(f"unsupported mode: {self.mode}")


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="scalar-snapshot-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="trusted",
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def test_behavioral_provider_id_scalar_is_rejected_at_registration() -> None:
    gateway = ModelGateway()

    with pytest.raises(ValueError, match="provider_id must be canonical text"):
        gateway.register(_CapabilitiesScalarProvider())

    assert gateway.providers() == ()


@pytest.mark.parametrize(
    "mode",
    (
        "model-str-subclass",
        "text-str-subclass",
        "provider-str-subclass",
        "request-str-subclass",
        "usage-int-subclass",
        "latency-float-subclass",
    ),
)
def test_provider_owned_scalar_subclasses_fail_before_completed_audit(mode: str) -> None:
    audit = _RecordingAudit()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(_ResponseScalarProvider(mode))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "trusted"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert [event for event, _payload in audit.events] == [
        "model.requested",
        "model.failed",
    ]
