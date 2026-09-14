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
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.retry import RetryPolicy


_PROVIDER_ID = "cloud-retry-fixture"
_MODEL_ID = "fixture-model"


class _FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


class _SequenceCloudProvider:
    def __init__(
        self,
        outcomes: list[ModelGatewayError | str],
        *,
        supports_hard_cancellation: bool = False,
    ) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[ModelRequest] = []
        self._capabilities = ProviderCapabilities(
            provider_id=_PROVIDER_ID,
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
            supports_hard_cancellation=supports_hard_cancellation,
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
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            latency_ms=1.0,
        )


def _definitions(store: SQLiteStore) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Complete the assigned task.",
        instructions="Return concise evidence.",
        model_profile="configured",
    )
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


def _harness(
    tmp_path: Path,
    outcomes: list[ModelGatewayError | str],
    *,
    supports_hard_cancellation: bool = False,
) -> tuple[
    SQLiteStore,
    TaskQueue,
    str,
    TaskRuntimeCoordinator,
    ModelGatewayAgentRuntime,
    _SequenceCloudProvider,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="qa-dev35", agent_id="worker")
    queue.transition(task.task_id, TaskState.READY)

    provider = _SequenceCloudProvider(
        outcomes,
        supports_hard_cancellation=supports_hard_cancellation,
    )
    gateway = ModelGateway()
    gateway.register(provider, default=True)
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=_definitions(store),
        provider_id=_PROVIDER_ID,
        provider_kind=ProviderKind.CLOUD,
        model=_MODEL_ID,
        timeout_seconds=3.0,
    )
    return (
        store,
        queue,
        task.task_id,
        TaskRuntimeCoordinator(queue, AuditLog(store)),
        runtime,
        provider,
    )


def _request(task_id: str, *, timeout_seconds: float = 3.0) -> RuntimeRequest:
    return RuntimeRequest(
        task_id=task_id,
        thread_id="thread-dev35",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"work": "fixture"},
        },
        timeout_seconds=timeout_seconds,
    )


def _cloud_retry_policy(*, delay: float = 1.0) -> RetryPolicy:
    return RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset(
            {RuntimeErrorCode.TRANSIENT, RuntimeErrorCode.TIMEOUT}
        ),
        base_delay_seconds=delay,
        max_delay_seconds=max(delay, 1.0),
        allow_fresh_retry=True,
    )


def _error(
    code: ModelErrorCode,
    *,
    retryable: bool,
    effect: ModelFailureEffect,
) -> ModelGatewayError:
    return ModelGatewayError(
        code,
        "provider detail must not control retry authority",
        provider_id=_PROVIDER_ID,
        retryable=retryable,
        failure_effect=effect,
    )


def test_429_retries_once_only_with_positive_backoff_and_no_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, task_id, coordinator, runtime, provider = _harness(
        tmp_path,
        [
            _error(
                ModelErrorCode.RATE_LIMITED,
                retryable=True,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            "ok-after-rate-limit",
        ],
    )
    clock = _FakeClock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)

    result = asyncio.run(
        coordinator.start(
            runtime,
            _request(task_id),
            retry_policy=_cloud_retry_policy(),
        )
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert len(provider.requests) == 2
    assert [request.provider_id for request in provider.requests] == [
        _PROVIDER_ID,
        _PROVIDER_ID,
    ]
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize(
    ("case", "failure", "supports_hard_cancellation"),
    [
        (
            "5xx-ambiguous-effect",
            _error(
                ModelErrorCode.UNAVAILABLE,
                retryable=True,
                effect=ModelFailureEffect.UNKNOWN,
            ),
            False,
        ),
        (
            "connection-failure-not-provider-retryable",
            _error(
                ModelErrorCode.PROVIDER_ERROR,
                retryable=False,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            False,
        ),
        (
            "timeout-without-hard-cancellation",
            _error(
                ModelErrorCode.TIMEOUT,
                retryable=False,
                effect=ModelFailureEffect.UNKNOWN,
            ),
            False,
        ),
        (
            "invalid-response-classification",
            _error(
                ModelErrorCode.PROVIDER_ERROR,
                retryable=False,
                effect=ModelFailureEffect.UNKNOWN,
            ),
            False,
        ),
        (
            "authentication",
            _error(
                ModelErrorCode.AUTHENTICATION,
                retryable=False,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            False,
        ),
        (
            "cancelled",
            _error(
                ModelErrorCode.CANCELLED,
                retryable=False,
                effect=ModelFailureEffect.UNKNOWN,
            ),
            True,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_cloud_fresh_retry_requires_provider_retryable_and_no_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    failure: ModelGatewayError,
    supports_hard_cancellation: bool,
) -> None:
    del case
    _, _, task_id, coordinator, runtime, provider = _harness(
        tmp_path,
        [failure, "must-not-be-called"],
        supports_hard_cancellation=supports_hard_cancellation,
    )
    clock = _FakeClock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)

    result = asyncio.run(
        coordinator.start(
            runtime,
            _request(task_id),
            retry_policy=_cloud_retry_policy(),
        )
    )

    assert result.outcome in {RuntimeOutcome.FAILED, RuntimeOutcome.CANCELLED}
    assert len(provider.requests) == 1
    assert clock.sleeps == []


def test_public_cancellation_during_backoff_is_durable_and_blocks_later_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue, task_id, coordinator, runtime, provider = _harness(
        tmp_path,
        [
            _error(
                ModelErrorCode.RATE_LIMITED,
                retryable=True,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            "must-not-run-after-cancel",
        ],
    )

    async def scenario() -> tuple[RuntimeOutcome, list[float]]:
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()
        sleeps: list[float] = []

        async def blocked_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            sleep_started.set()
            await release_sleep.wait()

        async def unexpected_runtime_cancel(*, task_id: str, thread_id: str) -> bool:
            del task_id, thread_id
            raise AssertionError("RETRYING cancellation must not call the provider runtime")

        monkeypatch.setattr(asyncio, "sleep", blocked_sleep)
        monkeypatch.setattr(runtime, "cancel", unexpected_runtime_cancel)
        running = asyncio.create_task(
            coordinator.start(
                runtime,
                _request(task_id),
                retry_policy=_cloud_retry_policy(),
            )
        )
        await sleep_started.wait()
        try:
            accepted = await coordinator.cancel(
                runtime,
                task_id=task_id,
                thread_id="thread-dev35",
            )
            assert accepted is True
            assert queue.get(task_id).state is TaskState.CANCELLED
            assert coordinator.sessions.get(task_id) is None

            cancel_records = [
                record
                for record in IdempotencyLedger(store).list_for_task(task_id)
                if record.operation_type == "runtime.cancel"
            ]
            assert len(cancel_records) == 1
            cancel_record = cancel_records[0]
            assert cancel_record.status is IdempotencyStatus.COMPLETED
            assert cancel_record.result is not None
            assert cancel_record.result["accepted"] is True
            assert cancel_record.result["runtime_call_skipped"] is True

            restarted_queue = TaskQueue(store)
            restarted_coordinator = TaskRuntimeCoordinator(restarted_queue, AuditLog(store))
            assert restarted_queue.get(task_id).state is TaskState.CANCELLED
            assert restarted_coordinator.sessions.get(task_id) is None
        finally:
            release_sleep.set()

        result = await running
        return result.outcome, sleeps

    outcome, sleeps = asyncio.run(scenario())

    assert outcome is RuntimeOutcome.CANCELLED
    assert len(provider.requests) == 1
    assert sleeps == [1.0]


def test_retry_backoff_consumes_original_inference_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, task_id, coordinator, runtime, provider = _harness(
        tmp_path,
        [
            _error(
                ModelErrorCode.RATE_LIMITED,
                retryable=True,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            "ok",
        ],
    )
    clock = _FakeClock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)

    result = asyncio.run(
        coordinator.start(
            runtime,
            _request(task_id, timeout_seconds=3.0),
            retry_policy=_cloud_retry_policy(),
        )
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert len(provider.requests) == 2
    first_budget = provider.requests[0].timeout_seconds
    second_budget = provider.requests[1].timeout_seconds
    assert clock.sleeps == [1.0]
    assert second_budget <= first_budget - 0.9


def test_cloud_retry_configuration_must_not_hot_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, task_id, coordinator, runtime, provider = _harness(
        tmp_path,
        [
            _error(
                ModelErrorCode.RATE_LIMITED,
                retryable=True,
                effect=ModelFailureEffect.NO_EFFECT,
            ),
            "must-not-be-hot-looped",
        ],
    )
    clock = _FakeClock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)

    result = asyncio.run(
        coordinator.start(
            runtime,
            _request(task_id),
            retry_policy=_cloud_retry_policy(delay=0.0),
        )
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert len(provider.requests) == 1
    assert clock.sleeps == []
