from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import Callable

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Settings:
    def __init__(self, on_set: Callable[[], None]) -> None:
        self._on_set = on_set
        self._temperature: float | None = None

    @property
    def temperature(self) -> float | None:
        return self._temperature

    @temperature.setter
    def temperature(self, value: float) -> None:
        self._on_set()
        self._temperature = value


class _Model:
    def __init__(self) -> None:
        self.id = "approved-artifact:1"
        self.alias = "approved-alias"
        self._cached = True
        self._loaded = True
        self.chat_calls = 0
        self.load_calls = 0
        self.client_calls = 0
        self.on_cached: Callable[[], None] = lambda: None
        self.on_client: Callable[[], None] = lambda: None
        self.on_temperature: Callable[[], None] = lambda: None

    @property
    def is_cached(self) -> bool:
        self.on_cached()
        return self._cached

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self.load_calls += 1
        self._loaded = True

    def get_chat_client(self) -> object:
        self.client_calls += 1
        self.on_client()
        model = self

        class _Client:
            settings = _Settings(model.on_temperature)

            def complete_chat(self, messages: object) -> object:
                del messages
                model.chat_calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="готово"))],
                    usage=None,
                )

        return _Client()


def _provider(model: _Model) -> FoundryLocalProvider:
    manager = SimpleNamespace(catalog=SimpleNamespace(get_model=lambda alias: model))
    return FoundryLocalProvider(
        default_model="approved-alias",
        manager_factory=lambda: manager,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="prechat-boundary",
        model="approved-alias",
        messages=(ModelMessage(role="user", content="Привіт"),),
        temperature=0.5,
        timeout_seconds=2.0,
    )


def test_cancel_during_client_creation_does_not_start_native_chat() -> None:
    abandoned = Event()
    model = _Model()
    model.on_client = abandoned.set
    with pytest.raises(ModelGatewayError) as caught:
        _provider(model)._complete_sync(_request(), abandoned)
    assert caught.value.code is ModelErrorCode.CANCELLED
    assert model.client_calls == 1
    assert model.chat_calls == 0


def test_cancel_during_temperature_setting_does_not_start_chat() -> None:
    abandoned = Event()
    model = _Model()
    model.on_temperature = abandoned.set
    with pytest.raises(ModelGatewayError) as caught:
        _provider(model)._complete_sync(_request(), abandoned)
    assert caught.value.code is ModelErrorCode.CANCELLED
    assert model.chat_calls == 0


def test_cancel_during_metadata_does_not_start_native_load() -> None:
    abandoned = Event()
    model = _Model()
    model._loaded = False
    model.on_cached = abandoned.set
    with pytest.raises(ModelGatewayError) as caught:
        _provider(model)._complete_sync(_request(), abandoned)
    assert caught.value.code is ModelErrorCode.CANCELLED
    assert model.load_calls == 0
    assert model.client_calls == 0
    assert model.chat_calls == 0


def test_variant_switch_during_temperature_setting_blocks_chat() -> None:
    abandoned = Event()
    model = _Model()
    model.on_temperature = lambda: setattr(model, "id", "other-artifact:2")
    with pytest.raises(ModelGatewayError) as caught:
        _provider(model)._complete_sync(_request(), abandoned)
    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert model.chat_calls == 0


def test_alias_switch_during_temperature_setting_blocks_chat() -> None:
    abandoned = Event()
    model = _Model()
    model.on_temperature = lambda: setattr(model, "alias", "other-alias")
    with pytest.raises(ModelGatewayError) as caught:
        _provider(model)._complete_sync(_request(), abandoned)
    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert model.chat_calls == 0


def test_stable_temperature_setting_still_completes() -> None:
    model = _Model()
    response = asyncio.run(_provider(model).complete(_request()))
    assert response.text == "готово"
    assert response.model == "approved-alias"
    assert model.chat_calls == 1


def test_async_cancellation_during_client_creation_prevents_delayed_chat() -> None:
    entered = Event()
    release = Event()
    model = _Model()

    def blocked_client() -> None:
        entered.set()
        assert release.wait(5.0)

    model.on_client = blocked_client
    provider = _provider(model)

    async def exercise() -> None:
        task = asyncio.create_task(provider.complete(_request()))
        try:
            assert await asyncio.to_thread(entered.wait, 1.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        # The native worker must finish before its deferred lock release.
        await asyncio.wait_for(provider._inference_lock.acquire(), timeout=2.0)
        provider._inference_lock.release()

    asyncio.run(exercise())
    assert model.client_calls == 1
    assert model.chat_calls == 0
