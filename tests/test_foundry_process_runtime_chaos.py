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
    ModelResourcePolicy,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.resources.contracts import ResourceSnapshot


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
    ) -> None:
        self.id = "qa-process-model:1"
        self.alias = "qa-process-model"
        self.is_cached = True
        self.is_loaded = True
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


class _MutableObserver:
    def __init__(self) -> None:
        self.snapshot_called = threading.Event()
        self.snapshot_value = ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=8 * 1024**3,
        )

    def snapshot(self) -> ResourceSnapshot:
        self.snapshot_called.set()
        return self.snapshot_value


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=request_id),),
        provider_id="foundry-local",
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=1.5,
    )


def _provider(
    manager: _Manager,
    *,
    resource_policy: ModelResourcePolicy | None = None,
    resource_observer: _MutableObserver | None = None,
) -> FoundryLocalProvider:
    return FoundryLocalProvider(
        default_model="qa-process-model",
        resource_policy=resource_policy,
        resource_observer=resource_observer,
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


@FOUNDRY_CONCURRENCY_BLOCKED
def test_resource_preflight_occurs_after_waiting_for_shared_native_authority() -> None:
    model = _SharedNativeModel(block_ids=frozenset({"blocker", "resource-queued"}))
    manager = _Manager(model)
    blocker = _provider(manager)
    observer = _MutableObserver()
    queued = _provider(
        manager,
        resource_policy=ModelResourcePolicy(max_cpu_percent=80.0),
        resource_observer=observer,
    )

    async def scenario() -> tuple[bool, object]:
        blocker_task = asyncio.create_task(blocker.complete(_request("blocker")))
        assert await asyncio.to_thread(model.wait_started, "blocker")

        queued_task = asyncio.create_task(queued.complete(_request("resource-queued")))
        early_snapshot = await asyncio.to_thread(observer.snapshot_called.wait, 0.5)
        observer.snapshot_value = ResourceSnapshot(
            cpu_percent=95.0,
            memory_percent=20.0,
            available_memory_bytes=8 * 1024**3,
        )
        model.release.set()

        blocker_response = await blocker_task
        assert blocker_response.request_id == "blocker"
        queued_result = await asyncio.gather(queued_task, return_exceptions=True)
        return early_snapshot, queued_result[0]

    early_snapshot, queued_result = asyncio.run(scenario())

    assert early_snapshot is False
    assert isinstance(queued_result, ModelGatewayError)
    assert queued_result.code is ModelErrorCode.RESOURCE_LIMIT
    assert "resource-queued" not in model.started_ids


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
