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


def _messages() -> tuple[ModelMessage, ...]:
    return (ModelMessage(role="user", content="parallel admission proof"),)


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
