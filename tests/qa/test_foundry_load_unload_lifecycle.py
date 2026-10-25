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


class LifecycleModel:
    """Deterministic Foundry SDK fake with explicit load/unload barriers."""

    def __init__(self) -> None:
        self.id = "stable-model-id"
        self.alias = "test-model"
        self.is_cached = True
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat,completion"
        self.supports_tool_calling = False
        self.settings = SimpleNamespace(temperature=None)

        self.load_count = 0
        self.unload_count = 0
        self.completion_count = 0
        self.block_load_on: set[int] = set()
        self.clean_fail_load_on: set[int] = set()
        self.dirty_fail_load_on: set[int] = set()
        self.not_ready_load_on: set[int] = set()
        self.identity_after_load: dict[int, str] = {}
        self.sticky_unload = False
        self.block_chat_on: set[int] = set()

        self.load_started = threading.Event()
        self.release_load = threading.Event()
        self.chat_started = threading.Event()
        self.release_chat = threading.Event()

    def get_path(self) -> str:
        return "C:/Nika QA Models/test-model"

    def load(self) -> None:
        self.load_count += 1
        call = self.load_count
        if call in self.block_load_on:
            self.load_started.set()
            if not self.release_load.wait(timeout=2.0):
                raise RuntimeError("test load barrier was not released")
        if call in self.identity_after_load:
            self.id = self.identity_after_load[call]
        if call in self.dirty_fail_load_on:
            self.is_loaded = True
            raise RuntimeError("synthetic load failure after SDK reports loaded")
        if call in self.clean_fail_load_on:
            self.is_loaded = False
            raise RuntimeError("synthetic load failure")
        if call in self.not_ready_load_on:
            self.is_loaded = False
            return
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_count += 1
        if not self.sticky_unload:
            self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                model.completion_count += 1
                call = model.completion_count
                if call in model.block_chat_on:
                    model.chat_started.set()
                    if not model.release_chat.wait(timeout=2.0):
                        raise RuntimeError("test chat barrier was not released")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                    ),
                )

        return Client()


class Catalog:
    def __init__(self, model: LifecycleModel | None) -> None:
        self.model = model

    def get_model(self, alias: str) -> LifecycleModel | None:
        if self.model is not None:
            self.model.alias = alias
        return self.model


class Manager:
    def __init__(self, model: LifecycleModel | None) -> None:
        self.catalog = Catalog(model)


def request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=5.0,
    )


def provider_for(
    model: LifecycleModel,
    *,
    expected_model_id: str | None = None,
) -> FoundryLocalProvider:
    manager = Manager(model)
    return FoundryLocalProvider(
        default_model="test-model",
        expected_model_id=expected_model_id,
        manager_factory=lambda: manager,
    )


async def wait_for_barrier(event: threading.Event) -> None:
    reached = await asyncio.to_thread(event.wait, 2.0)
    assert reached, "deterministic lifecycle barrier was not reached"


def test_repeated_inference_loads_ready_model_once() -> None:
    model = LifecycleModel()
    provider = provider_for(model)

    async def scenario() -> None:
        await provider.complete(request("first"))
        await provider.complete(request("second"))

    asyncio.run(scenario())

    assert model.load_count == 1
    assert model.completion_count == 2
    assert model.is_loaded is True


def test_concurrent_requests_share_one_load_transition() -> None:
    model = LifecycleModel()
    model.block_load_on.add(1)
    provider = provider_for(model)

    async def scenario() -> None:
        first = asyncio.create_task(provider.complete(request("first")))
        second = asyncio.create_task(provider.complete(request("second")))
        await wait_for_barrier(model.load_started)
        assert model.load_count == 1
        model.release_load.set()
        responses = await asyncio.gather(first, second)
        assert {response.request_id for response in responses} == {"first", "second"}

    asyncio.run(scenario())

    assert model.load_count == 1
    assert model.completion_count == 2


def test_close_refuses_unload_while_inference_owns_model() -> None:
    model = LifecycleModel()
    model.block_chat_on.add(1)
    provider = provider_for(model)

    async def scenario() -> None:
        active = asyncio.create_task(provider.complete(request("active")))
        await wait_for_barrier(model.chat_started)
        with pytest.raises(RuntimeError, match="native work is active"):
            provider.close()
        assert model.unload_count == 0
        model.release_chat.set()
        await active

    asyncio.run(scenario())
    provider.close()

    assert model.unload_count == 1
    assert model.is_loaded is False


def test_clean_failed_load_can_retry_without_stale_ready() -> None:
    model = LifecycleModel()
    model.clean_fail_load_on.add(1)
    provider = provider_for(model)

    async def scenario() -> None:
        with pytest.raises(ModelGatewayError) as failed:
            await provider.complete(request("failed-load"))
        assert failed.value.code is ModelErrorCode.PROVIDER_ERROR
        assert model.load_count == 1
        assert model.completion_count == 0
        assert model.is_loaded is False

        response = await provider.complete(request("retry"))
        assert response.request_id == "retry"

    asyncio.run(scenario())

    assert model.load_count == 2
    assert model.completion_count == 1
    assert model.is_loaded is True


def test_close_then_new_provider_reloads_same_model_cleanly() -> None:
    model = LifecycleModel()
    manager = Manager(model)
    first = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: manager,
    )

    asyncio.run(first.complete(request("before-shutdown")))
    first.close()
    assert model.is_loaded is False

    restarted = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: manager,
    )
    response = asyncio.run(restarted.complete(request("after-restart")))
    restarted.close()

    assert response.request_id == "after-restart"
    assert model.load_count == 2
    assert model.unload_count == 2
    assert model.is_loaded is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "current main trusts SDK is_loaded after a load() exception; a failed load must not "
        "become stale READY without a new successful transition or fail-closed retry"
    ),
)
def test_failed_load_that_reports_loaded_is_not_promoted_to_ready() -> None:
    model = LifecycleModel()
    model.dirty_fail_load_on.add(1)
    provider = provider_for(model)

    async def scenario() -> None:
        with pytest.raises(ModelGatewayError):
            await provider.complete(request("dirty-failure"))
        assert model.load_count == 1
        assert model.completion_count == 0

        try:
            await provider.complete(request("retry"))
        except ModelGatewayError:
            return
        assert model.load_count >= 2, "successful retry must include a fresh load transition"

    asyncio.run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason="current main enters chat even when load() returns without establishing is_loaded/READY",
)
def test_load_must_establish_ready_before_chat() -> None:
    model = LifecycleModel()
    model.not_ready_load_on.add(1)
    provider = provider_for(model)

    with pytest.raises(ModelGatewayError):
        asyncio.run(provider.complete(request("not-ready")))

    assert model.completion_count == 0
    assert model.is_loaded is False


@pytest.mark.xfail(
    strict=True,
    reason="current main validates expected_model_id only before load, not after reload",
)
def test_model_identity_is_revalidated_after_reload() -> None:
    model = LifecycleModel()
    provider = provider_for(model, expected_model_id="stable-model-id")

    asyncio.run(provider.complete(request("first-generation")))
    provider.close()
    model.identity_after_load[2] = "swapped-model-id"

    with pytest.raises(ModelGatewayError) as changed:
        asyncio.run(provider.complete(request("second-generation")))

    assert changed.value.code is ModelErrorCode.INVALID_REQUEST
    assert model.completion_count == 1


@pytest.mark.xfail(
    strict=True,
    reason="current main drops Nika ownership even when unload() returns with is_loaded still true",
)
def test_close_rejects_unload_that_leaves_stale_ready() -> None:
    model = LifecycleModel()
    provider = provider_for(model)
    asyncio.run(provider.complete(request("load")))
    model.sticky_unload = True

    with pytest.raises(ModelGatewayError) as failed:
        provider.close()

    assert failed.value.code is ModelErrorCode.PROVIDER_ERROR
    assert model.is_loaded is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "current main cannot stop the native load, but it also lacks an abandonment check between "
        "load completion and chat, so cancelled load can still execute inference"
    ),
)
def test_cancel_during_load_does_not_reach_chat_before_retry() -> None:
    model = LifecycleModel()
    model.block_load_on.add(1)
    provider = provider_for(model)

    async def scenario() -> None:
        first = asyncio.create_task(provider.complete(request("cancelled")))
        await wait_for_barrier(model.load_started)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        model.release_load.set()
        response = await provider.complete(request("retry"))
        assert response.request_id == "retry"

    asyncio.run(scenario())

    assert model.completion_count == 1, "only the explicit retry may enter chat"
