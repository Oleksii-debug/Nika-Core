from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


FOUNDRY_OWNER_BLOCKED = pytest.mark.xfail(
    strict=True,
    reason=(
        "current main coordinates Foundry native work per provider instance; DEV18 #182 owns "
        "the process-wide coordinator. Strict XPASS forces conversion after integration."
    ),
)


class _SharedNativeModel:
    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        delay: float = 0.0,
    ) -> None:
        self.id = "qa-process-model:1"
        self.alias = "qa-process-model"
        self.is_cached = True
        self.is_loaded = True
        self.settings = SimpleNamespace(temperature=None)
        self.gate = gate
        self.delay = delay
        self.started = threading.Event()
        self._lock = threading.Lock()
        self.completion_count = 0
        self.active = 0
        self.max_active = 0

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, _messages: list[dict[str, str]]) -> object:
                with model._lock:
                    model.completion_count += 1
                    model.active += 1
                    model.max_active = max(model.max_active, model.active)
                model.started.set()
                try:
                    if model.delay:
                        time.sleep(model.delay)
                    if model.gate is not None:
                        model.gate.wait(timeout=2.0)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
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
    def __init__(self, model: _SharedNativeModel) -> None:
        self._model = model

    def get_model(self, alias: str) -> _SharedNativeModel:
        self._model.alias = alias
        return self._model


class _Manager:
    def __init__(self, model: _SharedNativeModel) -> None:
        self.catalog = _Catalog(model)


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="private"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=1.0,
    )


def _provider(manager: _Manager) -> FoundryLocalProvider:
    return FoundryLocalProvider(
        default_model="qa-process-model",
        manager_factory=lambda: manager,
    )


@FOUNDRY_OWNER_BLOCKED
def test_distinct_foundry_provider_instances_share_one_native_execution_slot() -> None:
    model = _SharedNativeModel(delay=0.05)
    manager = _Manager(model)
    first_provider = _provider(manager)
    second_provider = _provider(manager)

    async def scenario() -> None:
        first, second = await asyncio.gather(
            first_provider.complete(_request("first")),
            second_provider.complete(_request("second")),
        )
        assert first.request_id == "first"
        assert second.request_id == "second"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active == 1


@FOUNDRY_OWNER_BLOCKED
def test_cross_instance_cancel_while_waiting_never_starts_second_native_request() -> None:
    gate = threading.Event()
    model = _SharedNativeModel(gate=gate)
    manager = _Manager(model)
    first_provider = _provider(manager)
    second_provider = _provider(manager)

    async def scenario() -> None:
        first = asyncio.create_task(first_provider.complete(_request("first")))
        started = await asyncio.to_thread(model.started.wait, 1.0)
        assert started is True

        second = asyncio.create_task(second_provider.complete(_request("second")))
        await asyncio.sleep(0.05)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        gate.set()
        response = await first
        assert response.request_id == "first"
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert model.completion_count == 1
    assert model.max_active == 1
