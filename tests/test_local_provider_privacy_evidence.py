from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceMode, IntelligenceModeRouter
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
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint

_PROMPT_CANARY = "NIKA_LOCAL_PROMPT_SECRET_30"
_RESPONSE_CANARY = "NIKA_LOCAL_RESPONSE_SECRET_30"
_PATH_CANARY = r"C:\Users\private-profile\.nika\models\private-model"
_ENV_CANARY = "NIKA_LOCAL_API_TOKEN=secret-value-30"
_HEADER_CANARY = "Authorization: Bearer secret-header-30"
_MODEL = "fixture-local-model"


class _LocalProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._fail:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"{_RESPONSE_CANARY} {_PATH_CANARY} {_ENV_CANARY} {_HEADER_CANARY}",
                provider_id=self.capabilities.provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        return ModelResponse(
            request_id=request.request_id,
            text=_RESPONSE_CANARY,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or _MODEL,
            usage=ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5),
            latency_ms=4.0,
        )


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=_PROMPT_CANARY),),
        model=_MODEL,
        provider_id="user-labelled-cloud-provider",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("user-labelled-cloud-fallback",),
        privacy=PrivacyClass.SENSITIVE,
        metadata={
            "local_path": _PATH_CANARY,
            "environment": _ENV_CANARY,
            "headers": _HEADER_CANARY,
        },
    )


def _audit_for(tmp_path: Path, *, fail: bool = False) -> tuple[AuditLog, _LocalProvider]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    gateway = ModelGateway(audit_log=audit)
    provider = _LocalProvider(fail=fail)
    gateway.register(provider)
    return audit, provider


def _durable_payloads(audit: AuditLog, request_id: str) -> tuple[object, ...]:
    return audit.list_for(entity_type="model_request", entity_id=request_id)


def _assert_private_material_absent(events: tuple[object, ...]) -> None:
    durable = json.dumps(
        [event.payload for event in events],  # type: ignore[attr-defined]
        ensure_ascii=False,
        sort_keys=True,
    )
    for canary in (
        _PROMPT_CANARY,
        _RESPONSE_CANARY,
        _PATH_CANARY,
        _ENV_CANARY,
        _HEADER_CANARY,
    ):
        assert canary not in durable


def test_local_success_audit_proves_validated_route_without_content(tmp_path: Path) -> None:
    audit, provider = _audit_for(tmp_path)
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)
    router = IntelligenceModeRouter(gateway=gateway)
    request = _request("local-privacy-success")

    response = asyncio.run(router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, request))

    assert response.text == _RESPONSE_CANARY
    assert len(provider.requests) == 1
    assert provider.requests[0].provider_id == "foundry-local"
    assert provider.requests[0].fallback_provider_ids == ()

    events = _durable_payloads(audit, request.request_id)
    assert [event.event_type for event in events] == ["model.requested", "model.completed"]
    requested, completed = events
    assert requested.entity_id == request.request_id
    assert requested.payload["provider_id"] == "foundry-local"
    assert requested.payload["provider_kind"] == ProviderKind.LOCAL.value
    assert requested.payload["model_fingerprint"] == model_identity_fingerprint(_MODEL)
    assert set(requested.payload) == {
        "provider_id",
        "provider_kind",
        "privacy",
        "model_fingerprint",
        "attempt",
    }
    assert completed.entity_id == request.request_id
    assert completed.payload["provider_id"] == requested.payload["provider_id"]
    assert completed.payload["model_fingerprint"] == requested.payload["model_fingerprint"]
    assert set(completed.payload) == {
        "provider_id",
        "model_fingerprint",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "attempt",
    }
    _assert_private_material_absent(events)


def test_local_failure_audit_is_terminal_and_redacted(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    gateway = ModelGateway(audit_log=audit)
    provider = _LocalProvider(fail=True)
    gateway.register(provider)
    router = IntelligenceModeRouter(gateway=gateway)
    request = _request("local-privacy-failure")

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, request))

    assert caught.value.code is ModelErrorCode.UNAVAILABLE
    assert _HEADER_CANARY not in str(caught.value)
    events = _durable_payloads(audit, request.request_id)
    assert [event.event_type for event in events] == ["model.requested", "model.failed"]
    requested, failed = events
    assert requested.payload["provider_id"] == "foundry-local"
    assert requested.payload["provider_kind"] == ProviderKind.LOCAL.value
    assert failed.payload == {
        "provider_id": "foundry-local",
        "model_fingerprint": model_identity_fingerprint(_MODEL),
        "code": ModelErrorCode.UNAVAILABLE.value,
        "failure_effect": ModelFailureEffect.NO_EFFECT.value,
    }
    _assert_private_material_absent(events)
