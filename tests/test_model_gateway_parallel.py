from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.parallel import (
    MAX_PARALLEL_MODEL_REQUESTS,
    ParallelModelStatus,
    complete_parallel,
)


def _request(
    request_id: str,
    *,
    provider_id: str,
    model: str,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=f"work for {request_id}"),),
        provider_id=provider_id,
        model=model,
    )


@dataclass
class _ConcurrencyTracker:
    active: int = 0
    max_active: int = 0
    calls: int = 0

    def enter(self) -> None:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        self.active -= 1


class _BarrierProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        tracker: _ConcurrencyTracker,
        overlap: asyncio.Event,
        release: asyncio.Event,
        overlap_target: int = 2,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self._tracker = tracker
        self._overlap = overlap
        self._release = release
        self._overlap_target = overlap_target

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._tracker.enter()
        if self._tracker.active >= self._overlap_target:
            self._overlap.set()
        try:
            await self._release.wait()
        finally:
            self._tracker.leave()
        return ModelResponse(
            request_id=request.request_id,
            text=f"done:{request.request_id}",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
        )


class _DelayedProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        delay_seconds: float,
        kind: ProviderKind = ProviderKind.CLOUD,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self._delay_seconds = delay_seconds

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self._delay_seconds)
        return ModelResponse(
            request_id=request.request_id,
            text=request.request_id,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
        )


class _FailingProvider:
    def __init__(self, *, provider_id: str) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelGatewayError(
            ModelErrorCode.RATE_LIMITED,
            "provider-controlled raw diagnostic that must not escape",
            provider_id=self.capabilities.provider_id,
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


def test_parallel_fanout_proves_mixed_local_and_cloud_requests_overlap() -> None:
    async def scenario() -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        overlap = asyncio.Event()
        release = asyncio.Event()
        tracker = _ConcurrencyTracker()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="local-a",
                kind=ProviderKind.LOCAL,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )
        gateway.register(
            _BarrierProvider(
                provider_id="cloud-b",
                kind=ProviderKind.CLOUD,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )

        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("local-request", provider_id="local-a", model="local-model"),
                    _request("cloud-request", provider_id="cloud-b", model="api-model"),
                ),
                max_parallel=2,
            )
        )
        await asyncio.wait_for(overlap.wait(), timeout=1.0)
        observed = tracker.max_active
        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        return (
            observed,
            tuple(item.request_id for item in result.outcomes),
            tuple(item.response.provider_id for item in result.completed if item.response),
        )

    max_active, request_ids, provider_ids = asyncio.run(scenario())

    assert max_active >= 2
    assert request_ids == ("local-request", "cloud-request")
    assert provider_ids == ("local-a", "cloud-b")


def test_parallel_fanout_does_not_globally_serialize_two_models_on_one_provider() -> None:
    async def scenario() -> int:
        overlap = asyncio.Event()
        release = asyncio.Event()
        tracker = _ConcurrencyTracker()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="ollama-like",
                kind=ProviderKind.LOCAL,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )
        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("model-a-request", provider_id="ollama-like", model="model-a"),
                    _request("model-b-request", provider_id="ollama-like", model="model-b"),
                ),
                max_parallel=2,
            )
        )
        await asyncio.wait_for(overlap.wait(), timeout=1.0)
        observed = tracker.max_active
        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert tuple(item.response.model for item in result.completed if item.response) == (
            "model-a",
            "model-b",
        )
        return observed

    assert asyncio.run(scenario()) >= 2


def test_parallel_fanout_respects_batch_concurrency_ceiling() -> None:
    async def scenario() -> int:
        tracker = _ConcurrencyTracker()
        release = asyncio.Event()
        overlap = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="bounded-provider",
                kind=ProviderKind.CLOUD,
                tracker=tracker,
                overlap=overlap,
                release=release,
                overlap_target=2,
            )
        )
        task = asyncio.create_task(
            complete_parallel(
                gateway,
                tuple(
                    _request(
                        f"request-{index}",
                        provider_id="bounded-provider",
                        model="bounded-model",
                    )
                    for index in range(4)
                ),
                max_parallel=2,
            )
        )
        await asyncio.wait_for(overlap.wait(), timeout=1.0)
        assert tracker.max_active == 2
        release.set()
        await asyncio.wait_for(task, timeout=1.0)
        return tracker.max_active

    assert asyncio.run(scenario()) == 2


def test_provider_wait_does_not_consume_global_slot_or_block_other_provider() -> None:
    async def scenario() -> int:
        tracker = _ConcurrencyTracker()
        release = asyncio.Event()
        overlap = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="slow-a",
                kind=ProviderKind.CLOUD,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )
        gateway.register(
            _BarrierProvider(
                provider_id="independent-b",
                kind=ProviderKind.LOCAL,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )

        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("a-1", provider_id="slow-a", model="api-a"),
                    _request("a-2", provider_id="slow-a", model="api-a"),
                    _request("b-1", provider_id="independent-b", model="local-b"),
                ),
                max_parallel=2,
                provider_limits={"slow-a": 1},
            )
        )
        await asyncio.wait_for(overlap.wait(), timeout=1.0)
        observed = tracker.max_active
        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert tuple(item.request_id for item in result.outcomes) == ("a-1", "a-2", "b-1")
        return observed

    assert asyncio.run(scenario()) == 2


def test_parallel_result_preserves_input_order_not_completion_order() -> None:
    gateway = ModelGateway()
    gateway.register(_DelayedProvider(provider_id="slow", delay_seconds=0.03))
    gateway.register(_DelayedProvider(provider_id="fast", delay_seconds=0.0))

    result = asyncio.run(
        complete_parallel(
            gateway,
            (
                _request("first", provider_id="slow", model="slow-model"),
                _request("second", provider_id="fast", model="fast-model"),
            ),
            max_parallel=2,
        )
    )

    assert tuple(item.request_id for item in result.outcomes) == ("first", "second")
    assert tuple(item.response.text for item in result.completed if item.response) == (
        "first",
        "second",
    )


def test_parallel_partial_failure_is_explicit_and_does_not_erase_success() -> None:
    gateway = ModelGateway()
    gateway.register(_FailingProvider(provider_id="rate-limited"))
    gateway.register(_DelayedProvider(provider_id="healthy", delay_seconds=0.0))

    result = asyncio.run(
        complete_parallel(
            gateway,
            (
                _request("failed-request", provider_id="rate-limited", model="api-a"),
                _request("healthy-request", provider_id="healthy", model="api-b"),
            ),
            max_parallel=2,
        )
    )

    assert result.fully_successful is False
    assert len(result.completed) == 1
    assert len(result.failed) == 1
    failed = result.outcomes[0]
    healthy = result.outcomes[1]
    assert failed.status is ParallelModelStatus.FAILED
    assert failed.response is None
    assert failed.failure is not None
    assert failed.failure.code is ModelErrorCode.RATE_LIMITED
    assert failed.failure.provider_id == "rate-limited"
    assert failed.failure.retryable is True
    assert failed.failure.failure_effect is ModelFailureEffect.NO_EFFECT
    assert not hasattr(failed.failure, "message")
    assert healthy.status is ParallelModelStatus.COMPLETED
    assert healthy.response is not None
    assert healthy.response.provider_id == "healthy"


def test_duplicate_request_identity_fails_before_any_provider_effect() -> None:
    async def scenario() -> int:
        overlap = asyncio.Event()
        release = asyncio.Event()
        tracker = _ConcurrencyTracker()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="provider",
                kind=ProviderKind.LOCAL,
                tracker=tracker,
                overlap=overlap,
                release=release,
            )
        )
        with pytest.raises(ValueError, match="request IDs must be unique"):
            await complete_parallel(
                gateway,
                (
                    _request("same", provider_id="provider", model="a"),
                    _request("same", provider_id="provider", model="b"),
                ),
            )
        return tracker.calls

    assert asyncio.run(scenario()) == 0


@pytest.mark.parametrize("value", [0, -1, MAX_PARALLEL_MODEL_REQUESTS + 1])
def test_parallel_concurrency_limit_is_bounded(value: int) -> None:
    gateway = ModelGateway()
    gateway.register(_DelayedProvider(provider_id="provider", delay_seconds=0.0))
    with pytest.raises(ValueError, match="max_parallel must be between"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", model="m"),),
                max_parallel=value,
            )
        )


def test_parallel_concurrency_limit_rejects_boolean() -> None:
    gateway = ModelGateway()
    gateway.register(_DelayedProvider(provider_id="provider", delay_seconds=0.0))
    with pytest.raises(TypeError, match="max_parallel must be an integer"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", model="m"),),
                max_parallel=True,
            )
        )


@pytest.mark.parametrize("value", [0, -1, MAX_PARALLEL_MODEL_REQUESTS + 1])
def test_provider_concurrency_limit_is_bounded(value: int) -> None:
    gateway = ModelGateway()
    gateway.register(_DelayedProvider(provider_id="provider", delay_seconds=0.0))
    with pytest.raises(ValueError, match="provider limit for provider must be between"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", model="m"),),
                provider_limits={"provider": value},
            )
        )


def test_provider_concurrency_limit_rejects_boolean() -> None:
    gateway = ModelGateway()
    gateway.register(_DelayedProvider(provider_id="provider", delay_seconds=0.0))
    with pytest.raises(TypeError, match="provider limit for provider must be an integer"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", model="m"),),
                provider_limits={"provider": True},
            )
        )


def test_unknown_provider_limit_fails_before_any_provider_effect() -> None:
    gateway = ModelGateway()
    provider = _DelayedProvider(provider_id="provider", delay_seconds=0.0)
    gateway.register(provider)
    with pytest.raises(ValueError, match="unknown provider"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", model="m"),),
                provider_limits={"typo-provider": 1},
            )
        )


def test_cancelling_batch_cancels_all_active_children() -> None:
    async def scenario() -> tuple[int, int]:
        overlap = asyncio.Event()
        never_release = asyncio.Event()
        tracker = _ConcurrencyTracker()
        gateway = ModelGateway()
        gateway.register(
            _BarrierProvider(
                provider_id="provider",
                kind=ProviderKind.CLOUD,
                tracker=tracker,
                overlap=overlap,
                release=never_release,
            )
        )
        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("one", provider_id="provider", model="a"),
                    _request("two", provider_id="provider", model="b"),
                ),
                max_parallel=2,
            )
        )
        await asyncio.wait_for(overlap.wait(), timeout=1.0)
        before_cancel = tracker.active
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        return before_cancel, tracker.active

    before_cancel, after_cancel = asyncio.run(scenario())
    assert before_cancel == 2
    assert after_cancel == 0
