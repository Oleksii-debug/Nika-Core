from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway


class FakeFoundryModel:
    def __init__(
        self,
        *,
        alias: str = "test-model",
        cached: bool = True,
        completion_delay: float = 0.0,
    ) -> None:
        self.alias = alias
        self.is_cached = cached
        self.is_loaded = False
        self.downloaded = False
        self.unloaded = False
        self.last_messages: list[dict[str, str]] = []
        self.settings = SimpleNamespace(temperature=None)
        self.completion_delay = completion_delay
        self._counter_lock = threading.Lock()
        self.active_completions = 0
        self.max_active_completions = 0

    def download(self) -> None:
        self.downloaded = True
        self.is_cached = True

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.unloaded = True
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                with model._counter_lock:
                    model.active_completions += 1
                    model.max_active_completions = max(
                        model.max_active_completions, model.active_completions
                    )
                try:
                    if model.completion_delay:
                        time.sleep(model.completion_delay)
                    model.last_messages = messages
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content="embedded: hello")
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=3,
                            completion_tokens=2,
                            total_tokens=5,
                        ),
                    )
                finally:
                    with model._counter_lock:
                        model.active_completions -= 1

        return Client()


class FakeCatalog:
    def __init__(self, model: FakeFoundryModel) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> FakeFoundryModel:
        self.requested_aliases.append(alias)
        self.model.alias = alias
        return self.model

    def get_loaded_models(self) -> list[FakeFoundryModel]:
        return [self.model] if self.model.is_loaded else []


class FakeManager:
    def __init__(self, model: FakeFoundryModel) -> None:
        self.catalog = FakeCatalog(model)


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "embedded-1",
        "messages": (ModelMessage(role="user", content="hello"),),
        "provider_id": "foundry-local",
        "privacy": PrivacyClass.SENSITIVE,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def test_foundry_local_runs_through_existing_gateway_without_cloud() -> None:
    model = FakeFoundryModel()
    manager = FakeManager(model)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: manager,
    )
    gateway = ModelGateway()
    gateway.register(provider)

    response = asyncio.run(gateway.complete(request(temperature=0.25)))

    assert response.text == "embedded: hello"
    assert response.provider_kind is ProviderKind.LOCAL
    assert response.provider_id == "foundry-local"
    assert response.model == "test-model"
    assert response.usage.total_tokens == 5
    assert model.settings.temperature == 0.25
    assert model.last_messages == [{"role": "user", "content": "hello"}]


def test_foundry_local_does_not_download_model_without_explicit_permission() -> None:
    model = FakeFoundryModel(cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )
    gateway = ModelGateway()
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request()))

    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert model.downloaded is False


def test_foundry_local_can_download_when_explicitly_enabled() -> None:
    model = FakeFoundryModel(cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        allow_download=True,
        manager_factory=lambda: FakeManager(model),
    )

    response = asyncio.run(provider.complete(request()))

    assert response.text == "embedded: hello"
    assert model.downloaded is True
    assert model.is_loaded is True


def test_foundry_local_honors_request_model_override_and_unloads() -> None:
    model = FakeFoundryModel()
    manager = FakeManager(model)
    provider = FoundryLocalProvider(
        default_model="small-model",
        manager_factory=lambda: manager,
    )

    response = asyncio.run(provider.complete(request(model="larger-model")))
    provider.close()

    assert manager.catalog.requested_aliases == ["larger-model"]
    assert response.model == "larger-model"
    assert model.unloaded is True


def test_foundry_local_maps_request_timeout_to_typed_gateway_error() -> None:
    model = FakeFoundryModel(completion_delay=0.05)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request(timeout_seconds=0.01)))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is True


def test_foundry_local_serializes_in_process_inference() -> None:
    model = FakeFoundryModel(completion_delay=0.03)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    async def run_parallel() -> None:
        first = provider.complete(request(request_id="first", timeout_seconds=1.0))
        second = provider.complete(request(request_id="second", timeout_seconds=1.0))
        responses = await asyncio.gather(first, second)
        assert [response.request_id for response in responses] == ["first", "second"]

    asyncio.run(run_parallel())

    assert model.max_active_completions == 1
