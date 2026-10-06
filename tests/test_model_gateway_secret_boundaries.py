from __future__ import annotations

import asyncio
import hashlib
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
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway

_CANARY = "NIKA_QA_MODEL_BOUNDARY_SECRET_c047ad"


def _request(*, model: str = "model-a") -> ModelRequest:
    return ModelRequest(
        request_id="model-secret-boundary",
        messages=(ModelMessage(role="user", content="synthetic prompt"),),
        model=model,
        provider_id="secret-fixture",
        privacy=PrivacyClass.PUBLIC,
    )


class _TypedFailureProvider:
    def __init__(self, *, include_provider_id: bool) -> None:
        self.include_provider_id = include_provider_id
        self.raw_error: ModelGatewayError | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="secret-fixture",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.raw_error = ModelGatewayError(
            ModelErrorCode.AUTHENTICATION,
            f"Authorization: Bearer {_CANARY}",
            provider_id=(self.capabilities.provider_id if self.include_provider_id else None),
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )
        raise self.raw_error


@pytest.mark.parametrize("include_provider_id", [False, True])
def test_typed_provider_failure_keeps_semantics_without_diagnostic_or_chain(
    include_provider_id: bool,
) -> None:
    provider = _TypedFailureProvider(include_provider_id=include_provider_id)
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error is not provider.raw_error
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.provider_id == "secret-fixture"
    assert error.retryable is False
    assert error.failure_effect is ModelFailureEffect.NO_EFFECT
    assert str(error) == "model provider authentication failed"
    assert _CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_untyped_provider_failure_drops_raw_exception_chain() -> None:
    class _UntypedFailureProvider(_TypedFailureProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            raise RuntimeError(f"provider diagnostic {_CANARY}")

    gateway = ModelGateway()
    gateway.register(_UntypedFailureProvider(include_provider_id=True))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert _CANARY not in str(error)
    assert _CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


class _SuccessfulProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="secret-fixture",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="synthetic response",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def test_success_audit_persists_only_stable_model_fingerprint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    gateway = ModelGateway(audit_log=audit)
    gateway.register(_SuccessfulProvider())
    model_id = f"https://models.invalid/model?api_key={_CANARY}"

    asyncio.run(gateway.complete(_request(model=model_id)))

    events = audit.list_for(
        entity_type="model_request",
        entity_id="model-secret-boundary",
    )
    durable = json.dumps(
        [event.payload for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )
    expected = "sha256:" + hashlib.sha256(model_id.encode()).hexdigest()
    fingerprints = [
        event.payload["model_fingerprint"]
        for event in events
        if event.event_type in {"model.requested", "model.completed"}
    ]

    assert _CANARY not in durable
    assert model_id not in durable
    assert fingerprints == [expected, expected]
