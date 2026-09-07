from __future__ import annotations

import asyncio
import json
import math
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
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway

_CANARY = "NIKA_PROVIDER_RESPONSE_CANARY_7f52c9"


class _FallbackProvider:
    def __init__(self) -> None:
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="fallback",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="fallback",
            provider_id="fallback",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


class _PoisonSuccessProvider:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.complete_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="trusted",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.complete_calls += 1
        valid = ModelResponse(
            request_id=request.request_id,
            text="provider output",
            provider_id="trusted",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
            usage=ModelUsage(input_tokens=1, output_tokens=2, total_tokens=3),
            latency_ms=1.5,
        )
        if self.mode == "wrong-request-id":
            return replace(valid, request_id=_CANARY)
        if self.mode == "wrong-provider-id":
            return replace(valid, provider_id=_CANARY)
        if self.mode == "wrong-provider-kind":
            return replace(valid, provider_kind=ProviderKind.CLOUD)
        if self.mode == "text-not-string":
            return replace(valid, text=[_CANARY])  # type: ignore[arg-type]
        if self.mode == "model-not-text":
            return replace(valid, model=123)  # type: ignore[arg-type]
        if self.mode == "usage-not-dto":
            return replace(valid, usage=_CANARY)  # type: ignore[arg-type]
        if self.mode == "usage-secret":
            return replace(
                valid,
                usage=ModelUsage(input_tokens=_CANARY),  # type: ignore[arg-type]
            )
        if self.mode == "usage-bool":
            return replace(valid, usage=ModelUsage(input_tokens=True))
        if self.mode == "usage-negative":
            return replace(valid, usage=ModelUsage(input_tokens=-1))
        if self.mode == "usage-huge":
            return replace(valid, usage=ModelUsage(input_tokens=10**10000))
        if self.mode == "usage-max":
            return replace(valid, usage=ModelUsage(input_tokens=(1 << 63) - 1))
        if self.mode == "latency-secret":
            return replace(valid, latency_ms=_CANARY)  # type: ignore[arg-type]
        if self.mode == "latency-nan":
            return replace(valid, latency_ms=math.nan)
        if self.mode == "latency-infinity":
            return replace(valid, latency_ms=math.inf)
        if self.mode == "latency-huge":
            return replace(valid, latency_ms=10**10000)
        if self.mode == "not-response":
            return {_CANARY: "not a response"}  # type: ignore[return-value]
        return valid


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="response-trust-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="trusted",
        fallback_provider_ids=("fallback",),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


@pytest.mark.parametrize(
    "mode",
    (
        "wrong-request-id",
        "wrong-provider-id",
        "wrong-provider-kind",
        "text-not-string",
        "model-not-text",
        "usage-not-dto",
        "usage-secret",
        "usage-bool",
        "usage-negative",
        "usage-huge",
        "latency-secret",
        "latency-nan",
        "latency-infinity",
        "latency-huge",
        "not-response",
    ),
)
def test_invalid_success_response_fails_before_completed_audit_or_fallback(
    tmp_path: Path,
    mode: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _PoisonSuccessProvider(mode)
    fallback = _FallbackProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "trusted"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert error.__cause__ is None
    assert error.__context__ is None
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0

    events = audit.list_for(
        entity_type="model_request",
        entity_id="response-trust-request",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    durable = json.dumps(
        [event.payload for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _CANARY not in durable
    assert '"failure_effect": "unknown"' in durable
    assert '"provider_id": "trusted"' in durable


def test_signed_64_bit_token_metadata_is_still_accepted(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _PoisonSuccessProvider("usage-max")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)

    response = asyncio.run(
        gateway.complete(
            replace(
                _request(),
                fallback_provider_ids=(),
            )
        )
    )

    assert response.usage.input_tokens == (1 << 63) - 1
    events = audit.list_for(
        entity_type="model_request",
        entity_id="response-trust-request",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.completed",
    ]


def test_valid_success_response_still_reaches_completed_audit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _PoisonSuccessProvider("valid")
    fallback = _FallbackProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    response = asyncio.run(gateway.complete(_request()))

    assert response.provider_id == "trusted"
    assert response.provider_kind is ProviderKind.LOCAL
    assert response.model == "fixture-model"
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0
    events = audit.list_for(
        entity_type="model_request",
        entity_id="response-trust-request",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.completed",
    ]
