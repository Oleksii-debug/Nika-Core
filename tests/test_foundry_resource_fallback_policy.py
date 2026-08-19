from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
    ModelResponse,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import DeterministicMockProvider
from nika_core.resources.contracts import ResourceSnapshot


class StaticObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=95.0,
            memory_percent=50.0,
            available_memory_bytes=8 * 1024**3,
        )


class Catalog:
    def get_model(self, alias: str) -> object:
        return SimpleNamespace(
            id="resource-test-cpu:1",
            alias=alias,
            is_cached=True,
            is_loaded=False,
        )


class Manager:
    catalog = Catalog()


def test_resource_limit_never_escalates_to_explicit_fallback_provider() -> None:
    fallback_called = False

    class FallbackProvider(DeterministicMockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal fallback_called
            fallback_called = True
            return await super().complete(request)

    gateway = ModelGateway()
    gateway.register(
        FoundryLocalProvider(
            default_model="resource-test",
            resource_policy=ModelResourcePolicy(max_cpu_percent=80.0),
            resource_observer=StaticObserver(),
            manager_factory=Manager,
        )
    )
    gateway.register(FallbackProvider(provider_id="fallback"))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            gateway.complete(
                ModelRequest(
                    request_id="resource-limit-no-fallback",
                    messages=(ModelMessage(role="user", content="hello"),),
                    provider_id="foundry-local",
                    fallback_provider_ids=("fallback",),
                    privacy=PrivacyClass.PRIVATE,
                )
            )
        )

    assert exc_info.value.code is ModelErrorCode.RESOURCE_LIMIT
    assert exc_info.value.retryable is False
    assert fallback_called is False
