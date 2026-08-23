from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import DeterministicMockProvider


class _AuditRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object] | None]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.events.append((event_type, entity_type, entity_id, payload))
        return len(self.events)


class _UnsafeCloudProvider(DeterministicMockProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="unsafe-cloud",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
        )


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "preflight-audit",
        "messages": (ModelMessage(role="user", content="payload-sentinel"),),
        "provider_id": "primary",
        "provider_kind": None,
        "privacy": PrivacyClass.PRIVATE,
        "fallback_provider_ids": ("unsafe-cloud",),
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def test_policy_denied_preflight_is_audited_without_payload_content() -> None:
    audit = _AuditRecorder()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(DeterministicMockProvider(provider_id="primary"))
    gateway.register(_UnsafeCloudProvider(provider_id="unsafe-cloud"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.POLICY_DENIED
    assert audit.events == [
        (
            "model.failed",
            "model_request",
            "preflight-audit",
            {
                "code": ModelErrorCode.POLICY_DENIED.value,
                "phase": "preflight",
                "provider_id": "unsafe-cloud",
            },
        )
    ]
    assert "payload-sentinel" not in repr(audit.events)


def test_ambiguous_preflight_route_audits_without_fabricated_provider_identity() -> None:
    audit = _AuditRecorder()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(DeterministicMockProvider(provider_id="first"))
    gateway.register(DeterministicMockProvider(provider_id="second"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                _request(
                    provider_id=None,
                    privacy=PrivacyClass.PUBLIC,
                    fallback_provider_ids=(),
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert audit.events == [
        (
            "model.failed",
            "model_request",
            "preflight-audit",
            {"code": ModelErrorCode.INVALID_REQUEST.value, "phase": "preflight"},
        )
    ]
