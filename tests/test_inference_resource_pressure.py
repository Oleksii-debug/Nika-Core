from __future__ import annotations

import asyncio

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


GIB = 1024**3


class StaticObserver:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> ResourceSnapshot:
        return self._snapshot


class BrokenObserver:
    def snapshot(self) -> ResourceSnapshot:
        raise RuntimeError("simulated resource observer failure")


class ExplodingManagerFactory:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> object:
        self.called = True
        raise AssertionError("model manager must not be created after failed resource preflight")


class OomModel:
    id = "huge-model:1"
    alias = "huge-model"
    is_cached = True
    is_loaded = False

    def __init__(self) -> None:
        self.load_called = False

    def load(self) -> None:
        self.load_called = True
        raise MemoryError("simulated provider OOM")

    def get_chat_client(self) -> object:
        raise AssertionError("chat client must not be created after failed model load")


class Catalog:
    def __init__(self, model: object) -> None:
        self._model = model

    def get_model(self, alias: str) -> object:
        assert alias == "huge-model"
        return self._model


class Manager:
    def __init__(self, model: object) -> None:
        self.catalog = Catalog(model)


class TrackingFallbackProvider(DeterministicMockProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="fallback")
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return await super().complete(request)


def request(*, fallback: bool = False) -> ModelRequest:
    return ModelRequest(
        request_id="resource-pressure",
        messages=(ModelMessage(role="user", content="hello"),),
        model="huge-model",
        provider_id="foundry-local",
        fallback_provider_ids=("fallback",) if fallback else (),
        privacy=PrivacyClass.PRIVATE,
    )


@pytest.mark.parametrize(
    ("snapshot", "policy"),
    (
        (
            ResourceSnapshot(
                cpu_percent=20.0,
                memory_percent=96.0,
                available_memory_bytes=8 * GIB,
            ),
            ModelResourcePolicy(max_memory_percent=90.0),
        ),
        (
            ResourceSnapshot(
                cpu_percent=20.0,
                memory_percent=50.0,
                available_memory_bytes=512 * 1024**2,
            ),
            ModelResourcePolicy(min_available_memory_bytes=4 * GIB),
        ),
    ),
)
def test_detectable_memory_pressure_blocks_before_model_manager_creation(
    snapshot: ResourceSnapshot,
    policy: ModelResourcePolicy,
) -> None:
    manager_factory = ExplodingManagerFactory()
    provider = FoundryLocalProvider(
        default_model="huge-model",
        resource_policy=policy,
        resource_observer=StaticObserver(snapshot),
        manager_factory=manager_factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    error = exc_info.value
    assert error.code is ModelErrorCode.RESOURCE_LIMIT
    assert error.provider_id == "foundry-local"
    assert error.retryable is False
    assert manager_factory.called is False


def test_resource_observer_failure_is_truthful_terminal_failure_before_model_use() -> None:
    manager_factory = ExplodingManagerFactory()
    provider = FoundryLocalProvider(
        default_model="huge-model",
        resource_policy=ModelResourcePolicy(min_available_memory_bytes=4 * GIB),
        resource_observer=BrokenObserver(),
        manager_factory=manager_factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "foundry-local"
    assert error.retryable is False
    assert manager_factory.called is False


def test_simulated_provider_oom_is_failure_not_recovery_success() -> None:
    model = OomModel()
    provider = FoundryLocalProvider(
        default_model="huge-model",
        manager_factory=lambda: Manager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "foundry-local"
    assert error.retryable is False
    assert model.load_called is True
    assert isinstance(error.__cause__, MemoryError)


def test_low_memory_resource_limit_does_not_silently_switch_provider_or_model() -> None:
    fallback = TrackingFallbackProvider()
    gateway = ModelGateway()
    gateway.register(
        FoundryLocalProvider(
            default_model="huge-model",
            resource_policy=ModelResourcePolicy(min_available_memory_bytes=4 * GIB),
            resource_observer=StaticObserver(
                ResourceSnapshot(
                    cpu_percent=20.0,
                    memory_percent=50.0,
                    available_memory_bytes=512 * 1024**2,
                )
            ),
            manager_factory=ExplodingManagerFactory(),
        )
    )
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request(fallback=True)))

    error = exc_info.value
    assert error.code is ModelErrorCode.RESOURCE_LIMIT
    assert error.provider_id == "foundry-local"
    assert fallback.calls == []


def test_provider_oom_does_not_silently_switch_provider_or_model() -> None:
    model = OomModel()
    fallback = TrackingFallbackProvider()
    gateway = ModelGateway()
    gateway.register(
        FoundryLocalProvider(
            default_model="huge-model",
            manager_factory=lambda: Manager(model),
        )
    )
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request(fallback=True)))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == "foundry-local"
    assert fallback.calls == []
