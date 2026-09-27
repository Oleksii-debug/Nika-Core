from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
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


@dataclass
class _FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CancellationSuppressingProvider:
    def __init__(self, *, block_after_cancel: bool) -> None:
        self._block_after_cancel = block_after_cancel
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.seen_timeout: float | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="stubborn",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.seen_timeout = request.timeout_seconds
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # ModelGateway must not trust provider cooperation after its own
            # terminal deadline/cancellation decision.
            self.cancel_seen.set()
            if self._block_after_cancel:
                await self.release.wait()
            return ModelResponse(
                request_id=request.request_id,
                text="late provider completion",
                provider_id=self.capabilities.provider_id,
                provider_kind=self.capabilities.kind,
                model=request.model or "fixture-model",
                usage=ModelUsage(),
                latency_ms=1.0,
            )


def _request(*, timeout_seconds: float = 1.0) -> ModelRequest:
    return ModelRequest(
        request_id="inference-deadline-contract",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="fixture-model",
        provider_id="stubborn",
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=timeout_seconds,
    )


async def _yield_loop(turns: int = 6) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def _run_late_completion_case() -> ModelResponse | BaseException:
    loop = asyncio.get_running_loop()
    clock = _FakeClock()
    original_time = loop.time
    loop.time = clock  # type: ignore[method-assign]
    try:
        provider = _CancellationSuppressingProvider(block_after_cancel=False)
        gateway = ModelGateway()
        gateway.register(provider)
        task = asyncio.create_task(gateway.complete(_request()))

        await provider.started.wait()
        assert provider.seen_timeout is not None
        assert 0 < provider.seen_timeout <= 1.0

        clock.advance(2.0)
        await _yield_loop()
        assert provider.cancel_seen.is_set()

        try:
            return await task
        except BaseException as exc:  # cancellation is part of this contract probe
            return exc
    finally:
        loop.time = original_time  # type: ignore[method-assign]


def test_gateway_deadline_is_terminal_even_if_provider_returns_after_cancel() -> None:
    outcome = asyncio.run(_run_late_completion_case())

    assert isinstance(outcome, ModelGatewayError)
    assert outcome.code is ModelErrorCode.TIMEOUT
    assert outcome.retryable is False


async def _run_noncooperative_timeout_case() -> tuple[
    bool,
    ModelResponse | BaseException,
]:
    loop = asyncio.get_running_loop()
    clock = _FakeClock()
    original_time = loop.time
    loop.time = clock  # type: ignore[method-assign]
    provider = _CancellationSuppressingProvider(block_after_cancel=True)
    gateway = ModelGateway()
    gateway.register(provider)
    task: asyncio.Task[ModelResponse] | None = None
    try:
        task = asyncio.create_task(gateway.complete(_request()))
        await provider.started.wait()

        clock.advance(2.0)
        await _yield_loop()
        assert provider.cancel_seen.is_set()
        finished_at_deadline = task.done()

        # Cleanup must not depend on the desired assertion. Release the fake
        # provider so a broken implementation cannot hang the test process.
        provider.release.set()
        await _yield_loop()
        try:
            outcome: ModelResponse | BaseException = await task
        except BaseException as exc:
            outcome = exc
        return finished_at_deadline, outcome
    finally:
        provider.release.set()
        if task is not None and not task.done():
            task.cancel()
            await _yield_loop()
        loop.time = original_time  # type: ignore[method-assign]


def test_gateway_does_not_wait_for_provider_to_honor_deadline_cancellation() -> None:
    finished_at_deadline, outcome = asyncio.run(_run_noncooperative_timeout_case())

    assert finished_at_deadline is True
    assert isinstance(outcome, ModelGatewayError)
    assert outcome.code is ModelErrorCode.TIMEOUT


async def _run_caller_cancel_case() -> tuple[bool, ModelResponse | BaseException]:
    provider = _CancellationSuppressingProvider(block_after_cancel=True)
    gateway = ModelGateway()
    gateway.register(provider)
    task = asyncio.create_task(gateway.complete(_request(timeout_seconds=30.0)))
    await provider.started.wait()

    task.cancel()
    await _yield_loop()
    assert provider.cancel_seen.is_set()
    cancellation_was_terminal = task.done()

    # Cleanup independently of the assertion.
    provider.release.set()
    await _yield_loop()
    try:
        outcome: ModelResponse | BaseException = await task
    except BaseException as exc:
        outcome = exc
    return cancellation_was_terminal, outcome


def test_caller_cancellation_cannot_be_suppressed_into_late_success() -> None:
    cancellation_was_terminal, outcome = asyncio.run(_run_caller_cancel_case())

    assert cancellation_was_terminal is True
    assert isinstance(outcome, asyncio.CancelledError)
