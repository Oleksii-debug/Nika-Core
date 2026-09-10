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


FOUNDRY_CONCURRENCY_BLOCKED = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "live main coordinates Foundry native work per provider instance; incumbent #272 owns "
        "the process-wide coordinator, while active #700 currently owns foundry_local.py"
    ),
)


class _SharedNativeModel:
    def __init__(
        self,
        *,
        block_ids: frozenset[str] = frozenset(),
        fail_ids: frozenset[str] = frozenset(),
        initially_loaded: bool = True,
    ) -> None:
        self.id = "qa-process-model:1"
        self.alias = "qa-process-model"
        self.is_cached = True
        self.is_loaded = initially_loaded
        self.settings = SimpleNamespace(temperature=None)
        self.block_ids = block_ids
        self.fail_ids = fail_ids
        self.release = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self.started_ids: list[str] = []
        self.active = 0
        self.max_active = 0
        self.load_count = 0
        self.unload_count = 0

    def load(self) -> None:
        with self._lock:
            self.load_count += 1
            self.is_loaded = True

    def unload(self) -> None:
        with self._lock:
            self.unload_count += 1
            self.is_loaded = False

    def wait_started(self, request_id: str, timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: request_id in self.started_ids,
                timeout=timeout,
            )

    def wait_for_overlap(self, timeout: float = 0.5) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self.max_active > 1, timeout=timeout)

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                request_id = messages[0]["content"]
                with model._condition:
                    model.started_ids.append(request_id)
                    model.active += 1
                    model.max_active = max(model.max_active, model.active)
                    model.idle.clear()
                    model._condition.notify_all()
                try:
                    if request_id in model.block_ids and not model.release.wait(timeout=2.0):
                        raise RuntimeError("test release barrier timed out")
                    if request_id in model.fail_ids:
                        raise RuntimeError(f"synthetic failure for {request_id}")
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content=f"response:{request_id}")
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=1,
                            completion_tokens=1,
                            total_tokens=2,
                        ),
                    )
                finally:
                    with model._condition:
                        model.active -= 1
                        if model.active == 0:
                            model.idle.set()
                        model._condition.notify_all()

        return Client()


class _Catalog:
    def __init__(self, model: _SharedNativeModel) -> None:
        self._model = model

    def get_model(self, _alias: str) -> _SharedNativeModel:
        return self._model


class _Manager:
    def __init__(self, model: _SharedNativeModel) -> None:
        self.catalog = _Catalog(model)


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=request_id),),
        provider_id="foundry-local",
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=1.5,
    )


def _provider(manager: _Manager) -> FoundryLocalProvider:
    return FoundryLocalProvider(
        default_model="qa-process-model",
        manager_factory=lambda: manager,
    )


@FOUNDRY_CONCURRENCY_BLOCKED
def test_distinct_foundry_instances_never_overlap_shared_native_inference() -> None:
    model = _SharedNativeModel(block_ids=frozenset({"first", "second"}))
    manager = _Manager(model)
    first_provider = _provider(manager)
    second_provider = _provider(manager)

    async def scenario() -> tuple[bool, object, object]:
        first_task = asyncio.create_task(first_provider.complete(_request("first")))
        assert await asyncio.to_thread(model.wait_started, "first")

        second_task = asyncio.create_task(second_provider.complete(_request("second")))
        overlapped = await asyncio.to_thread(model.wait_for_overlap)
        model.release.set()
        first, second = await asyncio.gather(first_task, second_task)
        return overlapped, first, second

    overlapped, first, second = asyncio.run(scenario())

    assert overlapped is False
    assert model.max_active == 1
    assert first.request_id == "first"
    assert second.request_id == "second"
    assert first.text == "response:first"
    assert second.text == "response:second"


@FOUNDRY_CONCURRENCY_BLOCKED
def test_cross_instance_cancel_while_queued_never_starts_native_request() -> None:
    model = _SharedNativeModel(block_ids=frozenset({"first", "second"}))
    manager = _Manager(model)
    first_provider = _provider(manager)
    second_provider = _provider(manager)

    async def scenario() -> bool:
        first_task = asyncio.create_task(first_provider.complete(_request("first")))
        assert await asyncio.to_thread(model.wait_started, "first")

        second_task = asyncio.create_task(second_provider.complete(_request("second")))
        overlapped = await asyncio.to_thread(model.wait_for_overlap)
        second_task.cancel()
        model.release.set()

        with pytest.raises(asyncio.CancelledError):
            await second_task
        first = await first_task
        assert first.request_id == "first"
        assert await asyncio.to_thread(model.idle.wait, 1.0)
        return overlapped

    overlapped = asyncio.run(scenario())

    assert overlapped is False
    assert model.started_ids == ["first"]
    assert model.max_active == 1


def test_concurrent_foundry_failure_does_not_poison_sibling_response() -> None:
    model = _SharedNativeModel(fail_ids=frozenset({"bad"}))
    manager = _Manager(model)
    bad_provider = _provider(manager)
    good_provider = _provider(manager)

    async def scenario() -> tuple[object, object]:
        bad_task = asyncio.create_task(bad_provider.complete(_request("bad")))
        good_task = asyncio.create_task(good_provider.complete(_request("good")))
        bad, good = await asyncio.gather(bad_task, good_task, return_exceptions=True)
        return bad, good

    bad, good = asyncio.run(scenario())

    assert isinstance(bad, ModelGatewayError)
    assert bad.code is ModelErrorCode.PROVIDER_ERROR
    assert not isinstance(good, BaseException)
    assert good.request_id == "good"
    assert good.text == "response:good"


@FOUNDRY_CONCURRENCY_BLOCKED
def test_owner_close_cannot_unload_model_under_sibling_native_request() -> None:
    model = _SharedNativeModel(
        block_ids=frozenset({"sibling"}),
        initially_loaded=False,
    )
    manager = _Manager(model)
    owner = _provider(manager)
    sibling_provider = _provider(manager)

    async def scenario() -> BaseException | None:
        primed = await owner.complete(_request("owner-prime"))
        assert primed.request_id == "owner-prime"
        assert model.load_count == 1

        sibling_task = asyncio.create_task(sibling_provider.complete(_request("sibling")))
        assert await asyncio.to_thread(model.wait_started, "sibling")

        close_error: BaseException | None = None
        try:
            owner.close()
        except BaseException as exc:  # noqa: BLE001 - assertion captures exact public behavior
            close_error = exc
        finally:
            model.release.set()

        sibling = await sibling_task
        assert sibling.request_id == "sibling"
        return close_error

    close_error = asyncio.run(scenario())

    assert isinstance(close_error, RuntimeError)
    assert model.unload_count == 0
    assert model.is_loaded is True
