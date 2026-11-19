from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.parallel import complete_parallel


@dataclass
class _OverlapState:
    active: int = 0
    max_active: int = 0

    def enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        self.active -= 1


class _BarrierCloudRoute:
    def __init__(
        self,
        *,
        provider_id: str,
        overlap: _OverlapState,
        target: int,
        reached: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )
        self._overlap = overlap
        self._target = target
        self._reached = reached
        self._release = release

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._overlap.enter()
        if self._overlap.active >= self._target:
            self._reached.set()
        try:
            await self._release.wait()
        finally:
            self._overlap.leave()
        return ModelResponse(
            request_id=request.request_id,
            text=f"result:{request.request_id}",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
        )


def _request(
    request_id: str,
    *,
    provider_id: str,
    model: str,
    timeout_seconds: float = 10.0,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=f"work:{request_id}"),),
        provider_id=provider_id,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def test_high_api_fanout_can_overlap_150_requests_when_explicitly_admitted() -> None:
    async def scenario() -> tuple[int, int]:
        target = 150
        overlap = _OverlapState()
        reached = asyncio.Event()
        release = asyncio.Event()
        gateway = ModelGateway()
        gateway.register(
            _BarrierCloudRoute(
                provider_id="cloud-fanout",
                overlap=overlap,
                target=target,
                reached=reached,
                release=release,
            )
        )

        requests = tuple(
            _request(
                f"project-{index // 15:02d}:agent-{index % 15:02d}",
                provider_id="cloud-fanout",
                model="api-model",
            )
            for index in range(target)
        )
        task = asyncio.create_task(
            complete_parallel(gateway, requests, max_parallel=target)
        )
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        observed = overlap.max_active
        release.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        return observed, len(result.completed)

    observed, completed = asyncio.run(scenario())

    assert observed == 150
    assert completed == 150
