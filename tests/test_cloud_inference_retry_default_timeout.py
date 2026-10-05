from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.retry import RetryPolicy

_PROVIDER_ID = "cloud-default-timeout-fixture"
_MODEL_ID = "fixture-model"


class _SequenceCloudProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self._outcomes: list[ModelGatewayError | str] = [
            ModelGatewayError(
                ModelErrorCode.RATE_LIMITED,
                "retryable fixture",
                provider_id=_PROVIDER_ID,
                retryable=True,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            ),
            "must-not-run",
        ]
        self._capabilities = ProviderCapabilities(
            provider_id=_PROVIDER_ID,
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, ModelGatewayError):
            raise outcome
        return ModelResponse(
            request_id=request.request_id,
            text=outcome,
            provider_id=_PROVIDER_ID,
            provider_kind=ProviderKind.CLOUD,
            model=request.model or _MODEL_ID,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _runtime_harness(
    tmp_path: Path,
) -> tuple[str, TaskRuntimeCoordinator, ModelGatewayAgentRuntime, _SequenceCloudProvider]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="qa-default-timeout", agent_id="worker")
    queue.transition(task.task_id, TaskState.READY)

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

    provider = _SequenceCloudProvider()
    gateway = ModelGateway()
    gateway.register(provider, default=True)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id=_PROVIDER_ID,
        provider_kind=ProviderKind.CLOUD,
        model=_MODEL_ID,
        timeout_seconds=3.0,
    )
    return task.task_id, TaskRuntimeCoordinator(queue, AuditLog(store)), runtime, provider


def test_model_fresh_retry_without_explicit_total_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, coordinator, runtime, provider = _runtime_harness(tmp_path)
    sleeps: list[float] = []

    async def unexpected_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", unexpected_sleep)
    request = RuntimeRequest(
        task_id=task_id,
        thread_id="thread-default-timeout",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "fixture"},
        },
        timeout_seconds=None,
    )
    policy = RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        base_delay_seconds=1.0,
        max_delay_seconds=1.0,
        allow_fresh_retry=True,
    )

    result = asyncio.run(coordinator.start(runtime, request, retry_policy=policy))

    assert result.outcome is RuntimeOutcome.FAILED
    assert len(provider.requests) == 1
    assert 0.0 < provider.requests[0].timeout_seconds <= 3.0
    assert sleeps == []
