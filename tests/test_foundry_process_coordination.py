from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.resources.contracts import ResourceSnapshot


class SharedFoundryModel:
    def __init__(self, *, cached: bool = True) -> None:
        self.id = "shared-model-cpu:1"
        self.alias = "shared-model"
        self.is_cached = cached
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.load_count = 0
        self.unload_count = 0
        self.download_count = 0
        self.completion_count = 0
        self.active_completions = 0
        self.max_active_completions = 0
        self.completion_started = threading.Event()
        self.completion_release = threading.Event()
        self.completion_release.set()
        self.download_started = threading.Event()
        self.download_release = threading.Event()
        self.download_release.set()
        self._counter_lock = threading.Lock()

    def get_path(self) -> str:
        return "C:/Nika Test Models/shared-model"

    def load(self) -> None:
        self.load_count += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_count += 1
        self.is_loaded = False

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_count += 1
        self.is_cached = True
        self.download_started.set()
        self.download_release.wait(1.0)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled")

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                with model._counter_lock:
                    model.active_completions += 1
                    model.completion_count += 1
                    model.max_active_completions = max(
                        model.max_active_completions,
                        model.active_completions,
                    )
                model.completion_started.set()
                try:
                    model.completion_release.wait(1.0)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                        usage=SimpleNamespace(
                            prompt_tokens=2,
                            completion_tokens=1,
                            total_tokens=3,
                        ),
                    )
                finally:
                    with model._counter_lock:
                        model.active_completions -= 1

        return Client()


class SharedCatalog:
    def __init__(self, model: SharedFoundryModel) -> None:
        self.model = model

    def get_model(self, alias: str) -> SharedFoundryModel | None:
        return self.model if alias == self.model.alias else None


class SharedManager:
    def __init__(self, model: SharedFoundryModel) -> None:
        self.catalog = SharedCatalog(model)


class MutableObserver:
    def __init__(self) -> None:
        self.snapshot_value = ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=8 * 1024**3,
        )

    def snapshot(self) -> ResourceSnapshot:
        return self.snapshot_value


def provider(
    model: SharedFoundryModel,
    *,
    resource_policy: ModelResourcePolicy | None = None,
    observer: MutableObserver | None = None,
) -> FoundryLocalProvider:
    manager = SharedManager(model)
    return FoundryLocalProvider(
        default_model=model.alias,
        expected_model_id=model.id,
        resource_policy=resource_policy,
        resource_observer=observer,
        manager_factory=lambda: manager,
    )


def request(request_id: str, *, timeout_seconds: float = 1.0) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=timeout_seconds,
    )


def authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="shared-model",
        license_reference="MODEL-LICENSE-REVIEW-SHARED",
        expected_model_id="shared-model-cpu:1",
    )


def test_separate_provider_instances_serialize_process_wide_inference() -> None:
    model = SharedFoundryModel()
    first_provider = provider(model)
    second_provider = provider(model)

    async def scenario() -> None:
        first, second = await asyncio.gather(
            first_provider.complete(request("first")),
            second_provider.complete(request("second")),
        )
        assert first.text == "ready"
        assert second.text == "ready"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active_completions == 1


def test_owner_cannot_unload_while_another_provider_is_using_model() -> None:
    model = SharedFoundryModel()
    owner = provider(model)
    consumer = provider(model)

    async def scenario() -> None:
        await owner.complete(request("owner-load"))
        assert model.load_count == 1

        model.completion_started.clear()
        model.completion_release.clear()
        active = asyncio.create_task(consumer.complete(request("consumer-active")))
        started = await asyncio.to_thread(model.completion_started.wait, 1.0)
        assert started is True

        with pytest.raises(RuntimeError, match="process-wide native work"):
            owner.close()
        assert model.unload_count == 0
        assert model.is_loaded is True

        model.completion_release.set()
        response = await active
        assert response.text == "ready"
        owner.close()

    asyncio.run(scenario())

    assert model.unload_count == 1
    assert model.is_loaded is False


def test_queued_timed_out_inference_is_abandoned_before_native_execution() -> None:
    model = SharedFoundryModel()
    blocker = provider(model)
    queued = provider(model)

    async def scenario() -> None:
        model.completion_started.clear()
        model.completion_release.clear()
        active = asyncio.create_task(blocker.complete(request("active")))
        started = await asyncio.to_thread(model.completion_started.wait, 1.0)
        assert started is True

        with pytest.raises(ModelGatewayError) as exc_info:
            await queued.complete(request("queued-timeout", timeout_seconds=0.01))
        assert exc_info.value.code is ModelErrorCode.TIMEOUT
        assert exc_info.value.retryable is False

        model.completion_release.set()
        assert (await active).text == "ready"

        # This follow-up is also a synchronization barrier: it cannot acquire the
        # provider slot until the timed-out queued worker has observed abandonment.
        response = await queued.complete(request("after-timeout"))
        assert response.text == "ready"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active_completions == 1


def test_cached_flag_cannot_publish_cross_provider_download_success_before_native_exit() -> None:
    model = SharedFoundryModel(cached=False)
    first_provider = provider(model)
    second_provider = provider(model)
    model.download_release.clear()

    async def scenario() -> None:
        first = asyncio.create_task(
            first_provider.download_model(authorization(), timeout_seconds=1.0)
        )
        started = await asyncio.to_thread(model.download_started.wait, 1.0)
        assert started is True
        assert model.is_cached is True

        second = asyncio.create_task(
            second_provider.download_model(authorization(), timeout_seconds=1.0)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.02)

        model.download_release.set()
        first_evidence, second_evidence = await asyncio.gather(first, second)
        assert first_evidence.cached is True
        assert second_evidence.cached is True

    asyncio.run(scenario())

    assert model.download_count == 1


def test_resource_preflight_occurs_after_process_wide_queue_wait() -> None:
    model = SharedFoundryModel()
    blocker = provider(model)
    observer = MutableObserver()
    queued = provider(
        model,
        resource_policy=ModelResourcePolicy(max_cpu_percent=80.0),
        observer=observer,
    )

    async def scenario() -> None:
        model.completion_started.clear()
        model.completion_release.clear()
        active = asyncio.create_task(blocker.complete(request("resource-blocker")))
        started = await asyncio.to_thread(model.completion_started.wait, 1.0)
        assert started is True

        waiting = asyncio.create_task(queued.complete(request("resource-queued")))
        await asyncio.sleep(0)
        assert waiting.done() is False
        observer.snapshot_value = ResourceSnapshot(
            cpu_percent=95.0,
            memory_percent=20.0,
            available_memory_bytes=8 * 1024**3,
        )

        model.completion_release.set()
        assert (await active).text == "ready"
        with pytest.raises(ModelGatewayError) as exc_info:
            await waiting
        assert exc_info.value.code is ModelErrorCode.RESOURCE_LIMIT
        assert exc_info.value.retryable is False

    asyncio.run(scenario())

    assert model.completion_count == 1


def test_precancelled_download_never_reaches_native_model_operation() -> None:
    model = SharedFoundryModel(cached=False)
    foundry = provider(model)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            foundry.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )

    assert exc_info.value.code is ModelErrorCode.CANCELLED
    assert model.download_count == 0


class MalformedUsageModel(SharedFoundryModel):
    def get_chat_client(self) -> object:
        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                    usage=SimpleNamespace(
                        prompt_tokens="2",
                        completion_tokens=1,
                        total_tokens=3,
                    ),
                )

        return Client()


def test_malformed_foundry_usage_is_rejected_instead_of_normalized_as_plausible() -> None:
    model = MalformedUsageModel()
    foundry = provider(model)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(foundry.complete(request("bad-usage")))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.retryable is False
