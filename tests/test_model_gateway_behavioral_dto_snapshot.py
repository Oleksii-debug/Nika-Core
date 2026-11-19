from __future__ import annotations

import asyncio
from collections import defaultdict

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class RecordingAudit:
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
        assert entity_id == "request-1"
        self.events.append((event_type, dict(payload or {})))
        return len(self.events)


class BehavioralCapabilities(ProviderCapabilities):
    def __init__(self) -> None:
        super().__init__(
            provider_id="trusted-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_tools=False,
            supports_streaming=False,
            supports_hard_cancellation=False,
        )
        object.__setattr__(self, "_reads", defaultdict(int))

    def __getattribute__(self, name: str) -> object:
        if name in {
            "provider_id",
            "kind",
            "supports_private_data",
            "supports_tools",
            "supports_streaming",
            "supports_hard_cancellation",
        }:
            reads = object.__getattribute__(self, "_reads")
            value = object.__getattribute__(self, name)
            reads[name] += 1
            if reads[name] > 1:
                if name == "provider_id":
                    return "spoofed-cloud"
                if name == "kind":
                    return ProviderKind.CLOUD
                return name != "supports_private_data"
            return value
        return object.__getattribute__(self, name)


class BehavioralUsage(ModelUsage):
    def __init__(self) -> None:
        super().__init__(input_tokens=3, output_tokens=4, total_tokens=7)
        object.__setattr__(self, "_reads", defaultdict(int))

    def __getattribute__(self, name: str) -> object:
        if name in {"input_tokens", "output_tokens", "total_tokens"}:
            reads = object.__getattribute__(self, "_reads")
            value = object.__getattribute__(self, name)
            reads[name] += 1
            if reads[name] > 1:
                return 10**100
            return value
        return object.__getattribute__(self, name)


class BehavioralResponse(ModelResponse):
    def __init__(self) -> None:
        super().__init__(
            request_id="request-1",
            text="trusted text",
            provider_id="trusted-local",
            provider_kind=ProviderKind.LOCAL,
            model="trusted-model",
            usage=BehavioralUsage(),
            latency_ms=12.5,
        )
        object.__setattr__(self, "_reads", defaultdict(int))

    def __getattribute__(self, name: str) -> object:
        if name in {
            "request_id",
            "text",
            "provider_id",
            "provider_kind",
            "model",
            "usage",
            "latency_ms",
        }:
            reads = object.__getattribute__(self, "_reads")
            value = object.__getattribute__(self, name)
            reads[name] += 1
            if reads[name] > 1:
                if name == "request_id":
                    return "poison-request"
                if name == "text":
                    return "POISON_RESPONSE_TEXT"
                if name == "provider_id":
                    return "spoofed-cloud"
                if name == "provider_kind":
                    return ProviderKind.CLOUD
                if name == "model":
                    return "poison-model"
                if name == "latency_ms":
                    return 999999.0
            return value
        return object.__getattribute__(self, name)


class BehavioralProvider:
    def __init__(self) -> None:
        self._capabilities = BehavioralCapabilities()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.provider_id == "trusted-local"
        assert request.fallback_provider_ids == ()
        return BehavioralResponse()


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="hello"),),
        model="trusted-model",
        provider_id="trusted-local",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=10.0,
    )


def test_capability_snapshot_does_not_retain_behavioral_provider_dto() -> None:
    audit = RecordingAudit()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(BehavioralProvider())

    response = asyncio.run(gateway.complete(_request()))

    assert type(response) is ModelResponse
    assert response.provider_id == "trusted-local"
    assert response.provider_kind is ProviderKind.LOCAL
    completed = [payload for event, payload in audit.events if event == "model.completed"]
    assert len(completed) == 1
    assert completed[0]["provider_id"] == "trusted-local"
    assert all("spoofed-cloud" not in repr(payload) for _event, payload in audit.events)


def test_success_snapshot_canonicalizes_behavioral_response_and_usage() -> None:
    audit = RecordingAudit()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(BehavioralProvider())

    response = asyncio.run(gateway.complete(_request()))

    assert type(response) is ModelResponse
    assert type(response.usage) is ModelUsage
    assert response.text == "trusted text"
    assert response.model == "trusted-model"
    assert response.usage == ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7)
    assert response.latency_ms == 12.5
    completed = [payload for event, payload in audit.events if event == "model.completed"]
    assert completed == [
        {
            "provider_id": "trusted-local",
            "model_fingerprint": completed[0]["model_fingerprint"],
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 7,
            "latency_ms": 12.5,
            "attempt": 1,
        }
    ]
    rendered = repr(audit.events)
    assert "POISON_RESPONSE_TEXT" not in rendered
    assert "poison-model" not in rendered
    assert "spoofed-cloud" not in rendered
