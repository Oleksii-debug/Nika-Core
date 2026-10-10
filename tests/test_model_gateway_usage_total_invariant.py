from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
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


class _UsageProvider:
    def __init__(self, provider_id: str, usage: ModelUsage) -> None:
        self.provider_id = provider_id
        self.usage = usage
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
            usage=self.usage,
            latency_ms=1.0,
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="usage-total-invariant",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="primary",
        fallback_provider_ids=("fallback",),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


@pytest.mark.parametrize(
    "usage",
    (
        ModelUsage(input_tokens=7, output_tokens=5, total_tokens=11),
        ModelUsage(input_tokens=7, output_tokens=None, total_tokens=6),
        ModelUsage(input_tokens=None, output_tokens=5, total_tokens=4),
    ),
)
def test_known_total_smaller_than_known_components_fails_before_completed_audit(
    tmp_path: Path,
    usage: ModelUsage,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _UsageProvider("primary", usage)
    fallback = _UsageProvider(
        "fallback",
        ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "primary"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0
    events = audit.list_for(
        entity_type="model_request",
        entity_id="usage-total-invariant",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]


def test_unknown_total_is_preserved_without_synthesizing_zero(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _UsageProvider(
        "primary",
        ModelUsage(input_tokens=7, output_tokens=5, total_tokens=None),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)

    response = asyncio.run(
        gateway.complete(
            ModelRequest(
                request_id="usage-total-unknown",
                messages=(ModelMessage(role="user", content="fixture"),),
                model="fixture-model",
                provider_id="primary",
                privacy=PrivacyClass.PUBLIC,
                timeout_seconds=2.0,
            )
        )
    )

    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 5
    assert response.usage.total_tokens is None
    events = audit.list_for(
        entity_type="model_request",
        entity_id="usage-total-unknown",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.completed",
    ]
