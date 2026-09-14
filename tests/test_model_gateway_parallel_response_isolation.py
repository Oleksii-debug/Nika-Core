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


class _WrongRequestProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="malformed",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=f"foreign:{request.request_id}",
            text="must not become completed",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


class _HealthySlowProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.completed = 0
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
        await asyncio.sleep(0.02)
        self.completed += 1
        return ModelResponse(
            request_id=request.request_id,
            text="healthy sibling survived",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


def _request(request_id: str, provider_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="response isolation proof"),),
        provider_id=provider_id,
        model="m",
        timeout_seconds=1.0,
    )


def test_foreign_response_request_id_fails_only_that_child() -> None:
    malformed = _WrongRequestProvider()
    healthy = _HealthySlowProvider()
    gateway = ModelGateway()
    gateway.register(malformed)
    gateway.register(healthy)

    result = asyncio.run(
        complete_parallel(
            gateway,
            (
                _request("bad-child", "malformed"),
                _request("good-child", "healthy"),
            ),
            max_parallel=2,
        )
    )

    assert malformed.calls == 1
    assert healthy.calls == 1
    assert healthy.completed == 1
    assert result.fully_successful is False

    bad = result.outcomes[0]
    assert bad.request_id == "bad-child"
    assert bad.status is ParallelModelStatus.FAILED
    assert bad.response is None
    assert bad.failure is not None
    assert bad.failure.code is ModelErrorCode.PROVIDER_ERROR
    assert bad.failure.provider_id == "malformed"
    assert bad.failure.retryable is False
    assert bad.failure.failure_effect is ModelFailureEffect.UNKNOWN

    good = result.outcomes[1]
    assert good.request_id == "good-child"
    assert good.status is ParallelModelStatus.COMPLETED
    assert good.response is not None
    assert good.response.request_id == "good-child"
    assert good.response.provider_id == "healthy"
    assert good.response.text == "healthy sibling survived"
