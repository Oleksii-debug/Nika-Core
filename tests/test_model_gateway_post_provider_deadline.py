from __future__ import annotations

import asyncio
import time
from dataclasses import replace
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
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _SlowTextResponse(ModelResponse):
    def __getattribute__(self, name: str) -> object:
        if name == "text":
            # A synchronous SDK/DTO getter can block inside response validation.
            time.sleep(0.25)
        return super().__getattribute__(name)


class _Primary:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="primary",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.mode == "blocked-provider":
            # asyncio.timeout cannot interrupt an event-loop-blocking SDK.
            time.sleep(0.25)
        elif self.mode == "async-provider":
            await asyncio.sleep(0.001)
        response_class = (
            _SlowTextResponse if self.mode == "blocked-getter" else ModelResponse
        )
        return response_class(
            request_id=request.request_id,
            text="Відповідь 🧠",
            provider_id="primary",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


class _Fallback:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="fallback",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="fallback",
            provider_id="fallback",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="post-provider-deadline",
        messages=(ModelMessage(role="user", content="перевірка"),),
        provider_id="primary",
        model="fixture-model",
        fallback_provider_ids=("fallback",),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=0.10,
    )


def _gateway(tmp_path: Path, mode: str) -> tuple[ModelGateway, AuditLog, _Primary, _Fallback]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _Primary(mode)
    fallback = _Fallback()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)
    return gateway, audit, primary, fallback


@pytest.mark.parametrize("mode", ("blocked-provider", "blocked-getter"))
def test_late_synchronous_success_is_not_durable_completion_or_safe_fallback(
    tmp_path: Path, mode: str
) -> None:
    gateway, audit, primary, fallback = _gateway(tmp_path, mode)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.TIMEOUT
    assert error.provider_id == "primary"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert error.__cause__ is None
    assert error.__context__ is None
    assert primary.calls == 1
    assert fallback.calls == 0
    events = audit.list_for(
        entity_type="model_request", entity_id="post-provider-deadline"
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload["code"] == "timeout"
    assert events[-1].payload["failure_effect"] == "unknown"


@pytest.mark.parametrize("mode", ("immediate", "async-provider"))
def test_in_budget_completion_preserves_unicode_and_existing_success_audit(
    tmp_path: Path, mode: str
) -> None:
    gateway, audit, primary, fallback = _gateway(tmp_path, mode)

    response = asyncio.run(
        gateway.complete(replace(_request(), timeout_seconds=2.0))
    )

    assert response.text == "Відповідь 🧠"
    assert response.provider_id == "primary"
    assert primary.calls == 1
    assert fallback.calls == 0
    events = audit.list_for(
        entity_type="model_request", entity_id="post-provider-deadline"
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.completed",
    ]
