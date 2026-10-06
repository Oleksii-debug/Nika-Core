from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore

from nika_core.model_gateway.contracts import (
    ModelAuditError,
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
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeRequest

_AUDIT_CANARY = "audit-secret-canary-704"
_PROVIDER_CANARY = "provider-secret-canary-704"


class _SelectiveFailAudit:
    def __init__(self, fail_event_type: str) -> None:
        self.fail_event_type = fail_event_type
        self.events: list[str] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        del entity_type, entity_id, payload
        self.events.append(event_type)
        if event_type == self.fail_event_type:
            raise RuntimeError(f"audit write failed: {_AUDIT_CANARY}")
        return len(self.events)


class _Provider:
    def __init__(
        self,
        provider_id: str,
        *,
        failure_effect: ModelFailureEffect | None = None,
        cancel: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.failure_effect = failure_effect
        self.cancel = cancel
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
        if self.cancel:
            raise asyncio.CancelledError()
        if self.failure_effect is not None:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"raw provider failure: {_PROVIDER_CANARY}",
                provider_id=self.provider_id,
                retryable=True,
                failure_effect=self.failure_effect,
            )
        return ModelResponse(
            request_id=request.request_id,
            text="fixture result",
            provider_id=self.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "fixture-model",
            usage=ModelUsage(input_tokens=1, output_tokens=2, total_tokens=3),
        )


def _request(*, fallback_provider_ids: tuple[str, ...] = ()) -> ModelRequest:
    return ModelRequest(
        request_id="audit-boundary-request",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="primary",
        fallback_provider_ids=fallback_provider_ids,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2,
    )


def _assert_audit_error(
    error: ModelAuditError,
    *,
    effect: ModelFailureEffect,
) -> None:
    assert not isinstance(error, ModelGatewayError)
    assert error.provider_id == "primary"
    assert error.failure_effect is effect
    assert str(error) == "model audit evidence could not be recorded"
    rendered = repr(error)
    assert _AUDIT_CANARY not in rendered
    assert _PROVIDER_CANARY not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_requested_audit_failure_blocks_provider_with_no_effect() -> None:
    audit = _SelectiveFailAudit("model.requested")
    provider = _Provider("primary")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(gateway.complete(_request()))

    _assert_audit_error(caught.value, effect=ModelFailureEffect.NO_EFFECT)
    assert provider.complete_calls == 0
    assert audit.events == ["model.requested"]


def test_completed_audit_failure_is_unknown_and_never_falls_back() -> None:
    audit = _SelectiveFailAudit("model.completed")
    primary = _Provider("primary")
    fallback = _Provider("fallback")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(
            gateway.complete(
                _request(fallback_provider_ids=("fallback",))
            )
        )

    _assert_audit_error(caught.value, effect=ModelFailureEffect.UNKNOWN)
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0
    assert audit.events == ["model.requested", "model.completed"]


@pytest.mark.parametrize(
    "effect",
    (ModelFailureEffect.NO_EFFECT, ModelFailureEffect.UNKNOWN),
)
def test_failed_audit_failure_preserves_attempt_effect_and_stops_fallback(
    effect: ModelFailureEffect,
) -> None:
    audit = _SelectiveFailAudit("model.failed")
    primary = _Provider("primary", failure_effect=effect)
    fallback = _Provider("fallback")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(
            gateway.complete(
                _request(fallback_provider_ids=("fallback",))
            )
        )

    _assert_audit_error(caught.value, effect=effect)
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0
    assert audit.events == ["model.requested", "model.failed"]


def test_fallback_audit_failure_stops_before_next_provider_effect() -> None:
    audit = _SelectiveFailAudit("model.fallback")
    primary = _Provider("primary", failure_effect=ModelFailureEffect.NO_EFFECT)
    fallback = _Provider("fallback")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(
            gateway.complete(
                _request(fallback_provider_ids=("fallback",))
            )
        )

    _assert_audit_error(caught.value, effect=ModelFailureEffect.NO_EFFECT)
    assert primary.complete_calls == 1
    assert fallback.complete_calls == 0
    assert audit.events == [
        "model.requested",
        "model.failed",
        "model.fallback",
    ]


def test_cancelled_audit_failure_is_unknown_not_raw_audit_exception() -> None:
    audit = _SelectiveFailAudit("model.cancelled")
    provider = _Provider("primary", cancel=True)
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(gateway.complete(_request()))

    _assert_audit_error(caught.value, effect=ModelFailureEffect.UNKNOWN)
    assert provider.complete_calls == 1
    assert audit.events == ["model.requested", "model.cancelled"]


def test_agent_runtime_does_not_reclassify_audit_failure_as_transient(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "runtime.db")
    store.initialize()
    definitions = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Complete the assigned task.",
        instructions="Return concise evidence.",
        model_profile="configured",
    )
    definitions.save_draft(compiler.compile(definition))
    definitions.activate(definition)

    audit = _SelectiveFailAudit("model.completed")
    provider = _Provider("primary")
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id="primary",
        provider_kind=ProviderKind.LOCAL,
        model="fixture-model",
    )
    request = RuntimeRequest(
        task_id="task-audit-infrastructure",
        thread_id="thread-audit-infrastructure",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "fixture"},
        },
    )

    with pytest.raises(ModelAuditError) as caught:
        asyncio.run(runtime.run(request))

    _assert_audit_error(caught.value, effect=ModelFailureEffect.UNKNOWN)
    assert provider.complete_calls == 1
    assert audit.events == ["model.requested", "model.completed"]
