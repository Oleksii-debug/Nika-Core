from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
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
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeRequest


class _ForgedCancellationProvider:
    def __init__(self) -> None:
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
        raise asyncio.CancelledError()


class _TypedForgedCancellationProvider:
    def __init__(self) -> None:
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
        raise ModelGatewayError(
            ModelErrorCode.CANCELLED,
            "provider-controlled cancellation",
            provider_id="trusted",
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


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
            text="fallback must not run",
            provider_id="fallback",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
        )


class _BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
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
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking provider must be cancelled")


def _request(*, fallback: bool) -> ModelRequest:
    return ModelRequest(
        request_id="provider-cancellation-authority",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="trusted",
        fallback_provider_ids=("fallback",) if fallback else (),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=5.0,
    )


def _audit(tmp_path: Path) -> AuditLog:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return AuditLog(store)


def test_provider_cannot_forge_caller_cancellation_or_fallback(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    primary = _ForgedCancellationProvider()
    fallback = _FallbackProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(fallback=True)))

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
        entity_id="provider-cancellation-authority",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload == {
        "provider_id": "trusted",
        "model_fingerprint": events[-1].payload["model_fingerprint"],
        "code": ModelErrorCode.PROVIDER_ERROR.value,
        "failure_effect": ModelFailureEffect.UNKNOWN.value,
    }


def test_typed_provider_cancellation_cannot_assert_caller_cancel_or_fallback(
    tmp_path: Path,
) -> None:
    audit = _audit(tmp_path)
    primary = _TypedForgedCancellationProvider()
    fallback = _FallbackProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request(fallback=True)))

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
        entity_id="provider-cancellation-authority",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.failed",
    ]
    assert events[-1].payload["code"] == ModelErrorCode.PROVIDER_ERROR.value
    assert events[-1].payload["failure_effect"] == ModelFailureEffect.UNKNOWN.value


def test_real_caller_cancellation_still_propagates_and_is_audited(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    provider = _BlockingProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    async def scenario() -> None:
        task = asyncio.create_task(gateway.complete(_request(fallback=False)))
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert provider.complete_calls == 1
    events = audit.list_for(
        entity_type="model_request",
        entity_id="provider-cancellation-authority",
    )
    assert [event.event_type for event in events] == [
        "model.requested",
        "model.cancelled",
    ]

def _definitions(store: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Complete the assigned task.",
        instructions="Return deterministic fixture evidence.",
        model_profile="configured",
    )
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


@pytest.mark.parametrize(
    "provider_factory",
    [_ForgedCancellationProvider, _TypedForgedCancellationProvider],
)
def test_forged_provider_cancellation_is_runtime_failure_not_task_cancel(
    tmp_path: Path,
    provider_factory: type[_ForgedCancellationProvider]
    | type[_TypedForgedCancellationProvider],
) -> None:
    store = SQLiteStore(tmp_path / "runtime.db")
    store.initialize()
    provider = provider_factory()
    gateway = ModelGateway()
    gateway.register(provider)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=_definitions(store),
        provider_id="trusted",
        provider_kind=ProviderKind.LOCAL,
        model="fixture-model",
    )
    request = RuntimeRequest(
        task_id="forged-provider-cancel-task",
        thread_id="forged-provider-cancel-thread",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "prove cancellation authority"},
        },
    )

    result = asyncio.run(runtime.run(request))

    assert provider.complete_calls == 1
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.TRANSIENT
    assert result.output["model_error_code"] == ModelErrorCode.PROVIDER_ERROR.value
    assert result.output["provider_id"] == "trusted"
    assert result.output["provider_retryable"] is False
    assert result.output["failure_effect"] == ModelFailureEffect.UNKNOWN.value

