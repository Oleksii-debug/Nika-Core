from __future__ import annotations

import asyncio
import json
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

_CANARY = "PROVIDER_ERROR_ENVELOPE_PRIVATE_CANARY_58c1"


class _HostileError(ModelGatewayError):
    def __init__(self, field: str) -> None:
        super().__init__(
            ModelErrorCode.AUTHENTICATION,
            _CANARY,
            provider_id="primary",
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )
        self._hostile_field = field

    def __getattribute__(self, name: str) -> object:
        if name == super().__getattribute__("_hostile_field"):
            raise RuntimeError(_CANARY)
        return super().__getattribute__(name)


class _HostileProviderID(str):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError(_CANARY)

    __hash__ = str.__hash__


class _Provider:
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
        del request
        self.calls += 1
        if self.mode.startswith("getter:"):
            raise _HostileError(self.mode.partition(":")[2])
        if self.mode == "hostile-provider-id":
            raise ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                _CANARY,
                provider_id=_HostileProviderID("primary"),
            )
        if self.mode == "invalid-code":
            raise ModelGatewayError(_CANARY, _CANARY)  # type: ignore[arg-type]
        raise ModelGatewayError(
            ModelErrorCode.AUTHENTICATION,
            _CANARY,
            provider_id="primary",
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
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
            model="fixture-model",
        )


@pytest.mark.parametrize(
    "mode",
    (
        "getter:code",
        "getter:retryable",
        "getter:failure_effect",
        "getter:provider_id",
        "hostile-provider-id",
        "invalid-code",
        "ordinary",
    ),
)
def test_untrusted_typed_error_envelope_never_leaks_or_bypasses_audit(
    tmp_path: Path, mode: str
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    primary = _Provider(mode)
    fallback = _Fallback()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)
    request = ModelRequest(
        request_id="error-envelope",
        messages=(ModelMessage(role="user", content="перевірка"),),
        provider_id="primary",
        fallback_provider_ids=("fallback",),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(request))

    error = caught.value
    expected_code = (
        ModelErrorCode.AUTHENTICATION
        if mode == "ordinary"
        else ModelErrorCode.PROVIDER_ERROR
    )
    assert error.code is expected_code
    assert error.provider_id == "primary"
    assert error.retryable is False
    assert error.failure_effect is (
        ModelFailureEffect.NO_EFFECT
        if mode == "ordinary"
        else ModelFailureEffect.UNKNOWN
    )
    assert _CANARY not in repr(error)
    assert _CANARY not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert primary.calls == 1
    assert fallback.calls == 0
    events = audit.list_for(entity_type="model_request", entity_id="error-envelope")
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload["code"] == expected_code.value
    durable = json.dumps([event.payload for event in events], ensure_ascii=False)
    assert _CANARY not in durable
