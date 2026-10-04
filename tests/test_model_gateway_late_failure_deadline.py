from __future__ import annotations

import asyncio
import json
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

_CANARY = "LATE_PROVIDER_PRIVATE_CANARY_619d"


class _SlowInvalidResponse(ModelResponse):
    def __getattribute__(self, name: str) -> object:
        if name == "text":
            time.sleep(0.65)
        return super().__getattribute__(name)


class _SlowErrorEnvelope(ModelGatewayError):
    def __getattribute__(self, name: str) -> object:
        if name == "code":
            time.sleep(0.65)
        return super().__getattribute__(name)


class _SlowThrowingResponse(ModelResponse):
    def __getattribute__(self, name: str) -> object:
        if name == "text":
            time.sleep(0.65)
            raise RuntimeError(_CANARY)
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
        if self.mode == "late-typed":
            time.sleep(0.65)
        if self.mode in {"late-typed", "quick-typed"}:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                _CANARY,
                provider_id="primary",
                retryable=True,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        if self.mode == "late-untyped":
            time.sleep(0.65)
            raise RuntimeError(_CANARY)
        if self.mode == "late-envelope":
            raise _SlowErrorEnvelope(
                ModelErrorCode.UNAVAILABLE,
                _CANARY,
                provider_id="primary",
                retryable=True,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        response_cls = {
            "late-invalid-response": _SlowInvalidResponse,
            "late-raising-getter": _SlowThrowingResponse,
        }.get(self.mode, ModelResponse)
        return response_cls(
            request_id=request.request_id,
            text="",
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
            text="Резервна відповідь",
            provider_id="fallback",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


def _fixture(
    tmp_path: Path, mode: str
) -> tuple[ModelGateway, AuditLog, _Primary, _Fallback, ModelRequest]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _Primary(mode)
    fallback = _Fallback()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)
    request = ModelRequest(
        request_id="late-failure",
        messages=(ModelMessage(role="user", content="перевірка"),),
        provider_id="primary",
        model="fixture-model",
        fallback_provider_ids=("fallback",),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=0.25,
    )
    return gateway, audit, primary, fallback, request


@pytest.mark.parametrize(
    "mode",
    (
        "late-typed",
        "late-untyped",
        "late-envelope",
        "late-invalid-response",
        "late-raising-getter",
    ),
)
def test_late_failures_are_timeouts_without_replay_or_private_diagnostics(
    tmp_path: Path, mode: str
) -> None:
    gateway, audit, primary, fallback, request = _fixture(tmp_path, mode)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(request))

    error = caught.value
    assert error.code is ModelErrorCode.TIMEOUT
    assert error.provider_id == "primary"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.UNKNOWN
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _CANARY not in repr(error)
    assert _CANARY not in str(error)
    assert primary.calls == 1
    assert fallback.calls == 0

    events = audit.list_for(entity_type="model_request", entity_id="late-failure")
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload["code"] == ModelErrorCode.TIMEOUT.value
    assert events[-1].payload["failure_effect"] == ModelFailureEffect.UNKNOWN.value
    assert _CANARY not in json.dumps(
        [event.payload for event in events], ensure_ascii=False
    )


def test_in_budget_no_effect_error_still_allows_existing_fallback(
    tmp_path: Path,
) -> None:
    gateway, audit, primary, fallback, request = _fixture(tmp_path, "quick-typed")

    response = asyncio.run(gateway.complete(replace(request, timeout_seconds=2.0)))

    assert response.provider_id == "fallback"
    assert response.text == "Резервна відповідь"
    assert primary.calls == 1
    assert fallback.calls == 1
    events = audit.list_for(entity_type="model_request", entity_id="late-failure")
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
        "model.fallback",
        "model.requested",
        "model.completed",
    ]
    assert events[1].payload["code"] == ModelErrorCode.UNAVAILABLE.value


def test_in_budget_invalid_response_remains_provider_error(
    tmp_path: Path,
) -> None:
    gateway, audit, primary, fallback, request = _fixture(
        tmp_path, "quick-invalid-response"
    )
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(replace(request, timeout_seconds=2.0)))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert primary.calls == 1
    assert fallback.calls == 0
    events = audit.list_for(entity_type="model_request", entity_id="late-failure")
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload["code"] == ModelErrorCode.PROVIDER_ERROR.value
