from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _BlockingModel:
    def __init__(self, gate: threading.Event) -> None:
        self.id = "cancel-model-cpu:1"
        self.alias = "cancel-model"
        self.is_cached = True
        self.is_loaded = True
        self.settings = SimpleNamespace(temperature=None)
        self.gate = gate
        self.started = threading.Event()
        self._lock = threading.Lock()
        self.completion_count = 0
        self.active = 0
        self.max_active = 0

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                with model._lock:
                    model.completion_count += 1
                    model.active += 1
                    model.max_active = max(model.max_active, model.active)
                model.started.set()
                try:
                    model.gate.wait(timeout=2.0)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                        usage=SimpleNamespace(
                            prompt_tokens=1,
                            completion_tokens=1,
                            total_tokens=2,
                        ),
                    )
                finally:
                    with model._lock:
                        model.active -= 1

        return Client()


class _Catalog:
    def __init__(self, model: _BlockingModel) -> None:
        self.model = model

    def get_model(self, alias: str) -> _BlockingModel | None:
        return self.model if alias == self.model.alias else None


class _Manager:
    def __init__(self, model: _BlockingModel) -> None:
        self.catalog = _Catalog(model)


def _provider(manager: _Manager) -> FoundryLocalProvider:
    return FoundryLocalProvider(
        default_model="cancel-model",
        expected_model_id="cancel-model-cpu:1",
        manager_factory=lambda: manager,
    )


def _request(request_id: str, *, timeout_seconds: float = 1.0) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=timeout_seconds,
    )


def test_cross_instance_cancel_while_queued_abandons_before_native_effect() -> None:
    gate = threading.Event()
    model = _BlockingModel(gate)
    manager = _Manager(model)
    first_provider = _provider(manager)
    queued_provider = _provider(manager)

    async def scenario() -> None:
        first = asyncio.create_task(first_provider.complete(_request("first")))
        assert await asyncio.to_thread(model.started.wait, 1.0) is True

        queued = asyncio.create_task(queued_provider.complete(_request("queued")))
        await asyncio.sleep(0.02)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued

        assert model.completion_count == 1
        gate.set()
        assert (await first).request_id == "first"

        # Barrier: the abandoned executor item must drain without entering chat.
        follow_up = await queued_provider.complete(_request("after-cancel"))
        assert follow_up.request_id == "after-cancel"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active == 1


def test_cancel_after_native_start_retains_slot_until_real_native_exit() -> None:
    gate = threading.Event()
    model = _BlockingModel(gate)
    manager = _Manager(model)
    started_provider = _provider(manager)
    other_provider = _provider(manager)

    async def scenario() -> None:
        active = asyncio.create_task(started_provider.complete(_request("started")))
        assert await asyncio.to_thread(model.started.wait, 1.0) is True

        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active

        assert started_provider.capabilities.supports_hard_cancellation is False
        assert model.active == 1
        with pytest.raises(RuntimeError, match="native work is active"):
            started_provider.close()

        with pytest.raises(ModelGatewayError) as exc_info:
            await other_provider.complete(_request("queued-timeout", timeout_seconds=0.01))
        assert exc_info.value.code is ModelErrorCode.TIMEOUT
        assert exc_info.value.retryable is False
        assert model.completion_count == 1

        gate.set()
        for _ in range(100):
            if model.active == 0:
                break
            await asyncio.sleep(0.005)
        assert model.active == 0

        # Barrier: abandoned queued work drains, then later inference can run.
        response = await other_provider.complete(_request("after-native-exit"))
        assert response.request_id == "after-native-exit"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active == 1
