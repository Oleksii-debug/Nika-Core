from __future__ import annotations

import asyncio
from collections.abc import Sequence

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
    MAX_PARALLEL_MODEL_BATCH_REQUESTS,
    ParallelModelStatus,
    complete_parallel,
)


class _CountingProvider:
    def __init__(self, provider_id: str) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
        )


class _UnavailableProvider(_CountingProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "untrusted provider detail",
            provider_id=self.capabilities.provider_id,
            retryable=True,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


class _BlockingProvider(_CountingProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(provider_id)
        self._entered = entered
        self._release = release

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self._entered.set()
        await self._release.wait()
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
        )


class _RefuseIterationOverBoundSequence(Sequence[ModelRequest]):
    """Proves oversized admission is rejected from len() without copying input."""

    def __init__(self) -> None:
        self.iteration_attempts = 0

    def __len__(self) -> int:
        return MAX_PARALLEL_MODEL_BATCH_REQUESTS + 1

    def __getitem__(self, index: int | slice) -> ModelRequest:
        del index
        self.iteration_attempts += 1
        raise AssertionError("oversized batch must not be materialized")


class _HostileInt(int):
    pass


class _HostileText(str):
    pass


def _messages() -> tuple[ModelMessage, ...]:
    return (ModelMessage(role="user", content="parallel admission proof"),)


def _request(
    request_id: str,
    *,
    provider_id: str,
    timeout_seconds: float,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=_messages(),
        provider_id=provider_id,
        timeout_seconds=timeout_seconds,
    )


def _assert_admission_timeout(result_index: int, result) -> None:  # type: ignore[no-untyped-def]
    outcome = result.outcomes[result_index]
    assert outcome.status is ParallelModelStatus.FAILED
    assert outcome.response is None
    assert outcome.failure is not None
    assert outcome.failure.code is ModelErrorCode.TIMEOUT
    assert outcome.failure.retryable is False
    assert outcome.failure.failure_effect is ModelFailureEffect.NO_EFFECT


def test_overbound_batch_rejects_before_copy_task_fanout_or_provider_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CountingProvider("provider")
    gateway = ModelGateway()
    gateway.register(provider)
    requests = _RefuseIterationOverBoundSequence()

    async def scenario() -> int:
        created_tasks = 0
        real_create_task = asyncio.create_task

        def tracked_create_task(coro, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal created_tasks
            created_tasks += 1
            return real_create_task(coro, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", tracked_create_task)
        with pytest.raises(ValueError, match="batch must contain at most"):
            await complete_parallel(gateway, requests, max_parallel=1)
        return created_tasks

    assert asyncio.run(scenario()) == 0
    assert requests.iteration_attempts == 0
    assert provider.calls == 0


def test_child_scheduler_delay_consumes_existing_request_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        provider = _CountingProvider("provider")
        gateway = ModelGateway()
        gateway.register(provider)
        real_create_task = asyncio.create_task

        def delayed_create_task(coro, *args, **kwargs):  # type: ignore[no-untyped-def]
            async def delayed():  # type: ignore[no-untyped-def]
                await asyncio.sleep(0.05)
                return await coro

            return real_create_task(delayed(), *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", delayed_create_task)
        result = await complete_parallel(
            gateway,
            (
                _request(
                    "scheduler-delayed-timeout",
                    provider_id="provider",
                    timeout_seconds=0.01,
                ),
            ),
            max_parallel=1,
        )
        return result, provider.calls

    result, provider_calls = asyncio.run(scenario())

    assert provider_calls == 0
    _assert_admission_timeout(0, result)
    assert result.outcomes[0].failure is not None
    assert result.outcomes[0].failure.provider_id == "provider"


def test_global_admission_wait_consumes_existing_request_timeout_budget() -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        entered = asyncio.Event()
        release = asyncio.Event()
        provider = _BlockingProvider(
            "provider",
            entered=entered,
            release=release,
        )
        gateway = ModelGateway()
        gateway.register(provider)
        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request(
                        "blocking",
                        provider_id="provider",
                        timeout_seconds=1.0,
                    ),
                    _request(
                        "queued-timeout",
                        provider_id="provider",
                        timeout_seconds=0.05,
                    ),
                ),
                max_parallel=1,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(0.1)
        assert provider.calls == 1
        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        return result, provider.calls

    result, provider_calls = asyncio.run(scenario())

    assert provider_calls == 1
    assert result.outcomes[0].status is ParallelModelStatus.COMPLETED
    _assert_admission_timeout(1, result)
    assert result.outcomes[1].failure is not None
    assert result.outcomes[1].failure.provider_id == "provider"


def test_provider_admission_wait_consumes_existing_request_timeout_budget() -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        entered = asyncio.Event()
        release = asyncio.Event()
        provider = _BlockingProvider(
            "provider",
            entered=entered,
            release=release,
        )
        gateway = ModelGateway()
        gateway.register(provider)
        task = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request(
                        "blocking",
                        provider_id="provider",
                        timeout_seconds=1.0,
                    ),
                    _request(
                        "provider-queued-timeout",
                        provider_id="provider",
                        timeout_seconds=0.05,
                    ),
                ),
                max_parallel=2,
                provider_limits={"provider": 1},
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(0.1)
        assert provider.calls == 1
        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        return result, provider.calls

    result, provider_calls = asyncio.run(scenario())

    assert provider_calls == 1
    assert result.outcomes[0].status is ParallelModelStatus.COMPLETED
    _assert_admission_timeout(1, result)
    assert result.outcomes[1].failure is not None
    assert result.outcomes[1].failure.provider_id == "provider"


def test_provider_limited_batch_rejects_hidden_fallback_before_any_effect() -> None:
    primary = _CountingProvider("primary")
    fallback = _CountingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)
    request = ModelRequest(
        request_id="with-fallback",
        messages=_messages(),
        provider_id="primary",
        fallback_provider_ids=("fallback",),
    )

    with pytest.raises(ValueError, match="explicit routes without fallback"):
        asyncio.run(
            complete_parallel(
                gateway,
                (request,),
                provider_limits={"primary": 1, "fallback": 1},
            )
        )

    assert primary.calls == 0
    assert fallback.calls == 0


def test_provider_limited_batch_requires_explicit_provider_before_any_effect() -> None:
    provider = _CountingProvider("cloud-default")
    gateway = ModelGateway()
    gateway.register(provider, default=True)
    request = ModelRequest(
        request_id="implicit-cloud",
        messages=_messages(),
        provider_kind=ProviderKind.CLOUD,
    )

    with pytest.raises(ValueError, match="require an explicit provider_id"):
        asyncio.run(
            complete_parallel(
                gateway,
                (request,),
                provider_limits={"cloud-default": 1},
            )
        )

    assert provider.calls == 0


def test_unlimited_parallel_batch_preserves_canonical_gateway_fallback_contract() -> None:
    primary = _UnavailableProvider("primary")
    fallback = _CountingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)
    request = ModelRequest(
        request_id="fallback-still-supported",
        messages=_messages(),
        provider_id="primary",
        fallback_provider_ids=("fallback",),
    )

    result = asyncio.run(complete_parallel(gateway, (request,)))

    assert result.fully_successful is True
    assert result.outcomes[0].response is not None
    assert result.outcomes[0].response.provider_id == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_max_parallel_rejects_integer_subclass_before_provider_effect() -> None:
    provider = _CountingProvider("provider")
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(TypeError, match="max_parallel must be an integer"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", timeout_seconds=1.0),),
                max_parallel=_HostileInt(1),
            )
        )

    assert provider.calls == 0


def test_provider_limit_rejects_integer_subclass_before_provider_effect() -> None:
    provider = _CountingProvider("provider")
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(TypeError, match="provider limit for provider must be an integer"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", timeout_seconds=1.0),),
                provider_limits={"provider": _HostileInt(1)},
            )
        )

    assert provider.calls == 0


def test_provider_limit_rejects_text_subclass_before_provider_effect() -> None:
    provider = _CountingProvider("provider")
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(TypeError, match="provider limit ID must be text"):
        asyncio.run(
            complete_parallel(
                gateway,
                (_request("request", provider_id="provider", timeout_seconds=1.0),),
                provider_limits={_HostileText("provider"): 1},
            )
        )

    assert provider.calls == 0
