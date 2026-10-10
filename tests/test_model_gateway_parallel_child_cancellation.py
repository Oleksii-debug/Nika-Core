from __future__ import annotations

import asyncio

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.parallel import ParallelModelStatus, complete_parallel


class _SelfCancellingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="self-cancelling",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("self-cancellation must interrupt the child")


class _HealthyProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="healthy",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        await asyncio.sleep(0)
        return ModelResponse(
            request_id=request.request_id,
            text="healthy sibling completed",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


def _request(request_id: str, provider_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="child cancellation isolation"),),
        provider_id=provider_id,
        model="m",
        timeout_seconds=2.0,
    )


def test_child_self_cancellation_does_not_cancel_independent_sibling() -> None:
    cancelling = _SelfCancellingProvider()
    healthy = _HealthyProvider()
    gateway = ModelGateway()
    gateway.register(cancelling)
    gateway.register(healthy)

    result = asyncio.run(
        complete_parallel(
            gateway,
            (
                _request("cancelled-child", "self-cancelling"),
                _request("healthy-child", "healthy"),
            ),
            max_parallel=2,
        )
    )

    assert cancelling.calls == 1
    assert healthy.calls == 1
    assert result.fully_successful is False

    cancelled = result.outcomes[0]
    assert cancelled.status is ParallelModelStatus.FAILED
    assert cancelled.response is None
    assert cancelled.failure is not None
    assert cancelled.failure.code is ModelErrorCode.CANCELLED
    assert cancelled.failure.provider_id == "self-cancelling"
    assert cancelled.failure.retryable is False
    assert cancelled.failure.failure_effect is ModelFailureEffect.UNKNOWN

    sibling = result.outcomes[1]
    assert sibling.status is ParallelModelStatus.COMPLETED
    assert sibling.response is not None
    assert sibling.response.provider_id == "healthy"
    assert sibling.response.text == "healthy sibling completed"
