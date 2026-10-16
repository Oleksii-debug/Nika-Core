from __future__ import annotations

import asyncio
import math
import threading
import time
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
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.resources.contracts import ResourceSnapshot


class FakeFoundryModel:
    def __init__(
        self,
        *,
        alias: str = "test-model",
        cached: bool = True,
        completion_delay: float = 0.0,
    ) -> None:
        self.id = "test-model-id"
        self.alias = alias
        self.is_cached = cached
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat,completion"
        self.supports_tool_calling = False
        self.downloaded = False
        self.download_cancel_event: threading.Event | None = None
        self.unloaded = False
        self.last_messages: list[dict[str, str]] = []
        self.settings = SimpleNamespace(temperature=None)
        self.completion_delay = completion_delay
        self._counter_lock = threading.Lock()
        self.active_completions = 0
        self.max_active_completions = 0
        self.completion_count = 0

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_cancel_event = cancel_event
        self.downloaded = True
        self.is_cached = True

    def get_path(self) -> str:
        return "C:/Nika Test Models/test-model"

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
                    model.completion_count += 1
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


class BlockingDownloadModel(FakeFoundryModel):
    def __init__(self) -> None:
        super().__init__(cached=False)
        self.download_started = threading.Event()
        self.release_download = threading.Event()

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_cancel_event = cancel_event
        self.download_started.set()
        if not self.release_download.wait(timeout=2.0):
            raise RuntimeError("download barrier was not released")
        self.downloaded = True
        self.is_cached = True


class CancelDuringEvidenceModel(FakeFoundryModel):
    def __init__(
        self,
        cancel_event: threading.Event,
        *,
        cached: bool,
    ) -> None:
        super().__init__(cached=cached)
        self._cancel_during_evidence = cancel_event

    def get_path(self) -> str:
        self._cancel_during_evidence.set()
        return super().get_path()


class FakeCatalog:
    def __init__(self, model: FakeFoundryModel | None) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> FakeFoundryModel | None:
        self.requested_aliases.append(alias)
        if self.model is not None:
            self.model.alias = alias
        return self.model

    def get_loaded_models(self) -> list[FakeFoundryModel]:
        if self.model is None:
            return []
        return [self.model] if self.model.is_loaded else []


class FakeManager:
    def __init__(self, model: FakeFoundryModel | None) -> None:
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


def authorization(**overrides: object) -> ModelDownloadAuthorization:
    values: dict[str, object] = {
        "provider_id": "foundry-local",
        "model": "test-model",
        "license_reference": "MODEL-LICENSE-REVIEW-123",
    }
    values.update(overrides)
    return ModelDownloadAuthorization(**values)  # type: ignore[arg-type]


def test_foundry_manager_initialization_is_singleton_under_thread_race() -> None:
    model = FakeFoundryModel()
    manager = FakeManager(model)
    factory_started = threading.Event()
    release_factory = threading.Event()
    calls: list[str] = []
    results: list[str] = []
    errors: list[BaseException] = []

    def manager_factory() -> object:
        calls.append("factory")
        factory_started.set()
        if not release_factory.wait(timeout=2.0):
            raise RuntimeError("manager factory barrier was not released")
        return manager

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )

    def inspect() -> None:
        try:
            results.append(provider.inspect_model().model_id)
        except BaseException as exc:  # noqa: BLE001 - test thread must preserve failures.
            errors.append(exc)

    first = threading.Thread(target=inspect)
    second = threading.Thread(target=inspect)
    first.start()
    assert factory_started.wait(timeout=2.0)
    second.start()
    release_factory.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert calls == ["factory"]
    assert results == ["test-model-id", "test-model-id"]


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
    assert provider.capabilities.supports_hard_cancellation is False


def test_foundry_provider_revalidates_resource_policy_at_construction() -> None:
    class Observer:
        def snapshot(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                cpu_percent=10.0,
                memory_percent=20.0,
                available_memory_bytes=1_000_000,
            )

    forged = ModelResourcePolicy(max_cpu_percent=50.0)
    object.__setattr__(forged, "max_cpu_percent", -1.0)

    with pytest.raises(ValueError, match="max_cpu_percent"):
        FoundryLocalProvider(
            default_model="test-model",
            resource_policy=forged,
            resource_observer=Observer(),
        )


def test_foundry_provider_rejects_nonfinite_resource_snapshot_before_manager() -> None:
    calls: list[str] = []

    class Observer:
        def snapshot(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                cpu_percent=math.nan,
                memory_percent=20.0,
                available_memory_bytes=1_000_000,
            )

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid resource evidence")

    provider = FoundryLocalProvider(
        default_model="test-model",
        resource_policy=ModelResourcePolicy(max_cpu_percent=90.0),
        resource_observer=Observer(),
        manager_factory=manager_factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert calls == []


def test_foundry_provider_rejects_behavioral_resource_values_without_execution() -> None:
    calls: list[str] = []

    class BehavioralFloat(float):
        def __float__(self) -> float:
            raise AssertionError("behavioral resource conversion must not execute")

        def __gt__(self, other: object) -> bool:
            raise AssertionError("behavioral resource comparison must not execute")

    class Observer:
        def snapshot(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                cpu_percent=BehavioralFloat(10.0),
                memory_percent=20.0,
                available_memory_bytes=1_000_000,
            )

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid resource evidence")

    provider = FoundryLocalProvider(
        default_model="test-model",
        resource_policy=ModelResourcePolicy(max_cpu_percent=90.0),
        resource_observer=Observer(),
        manager_factory=manager_factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert calls == []


def test_foundry_provider_rejects_invalid_available_memory_before_manager() -> None:
    calls: list[str] = []

    class Observer:
        def snapshot(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                cpu_percent=10.0,
                memory_percent=20.0,
                available_memory_bytes=-1,
            )

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid resource evidence")

    provider = FoundryLocalProvider(
        default_model="test-model",
        resource_policy=ModelResourcePolicy(min_available_memory_bytes=1),
        resource_observer=Observer(),
        manager_factory=manager_factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert calls == []


def test_foundry_provider_snapshots_resource_policy_before_inference() -> None:
    class Observer:
        def snapshot(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                cpu_percent=80.0,
                memory_percent=20.0,
                available_memory_bytes=1_000_000,
            )

    model = FakeFoundryModel()
    manager = FakeManager(model)
    supplied = ModelResourcePolicy(max_cpu_percent=50.0)
    provider = FoundryLocalProvider(
        default_model="test-model",
        resource_policy=supplied,
        resource_observer=Observer(),
        manager_factory=lambda: manager,
    )

    object.__setattr__(supplied, "max_cpu_percent", 100.0)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.code is ModelErrorCode.RESOURCE_LIMIT
    assert manager.catalog.requested_aliases == []


def test_foundry_complete_revalidates_forged_request_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid request")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )
    forged = request()
    object.__setattr__(
        forged,
        "messages",
        [ModelMessage(role="user", content="mutated")],
    )

    with pytest.raises(TypeError, match="request messages must be a canonical tuple"):
        asyncio.run(provider.complete(forged))

    assert calls == []


def test_foundry_complete_snapshots_request_before_waiting() -> None:
    async def scenario() -> None:
        model = FakeFoundryModel()
        manager = FakeManager(model)
        provider = FoundryLocalProvider(
            default_model="test-model",
            manager_factory=lambda: manager,
        )
        supplied = request(model="test-model", timeout_seconds=1.0)

        await provider._inference_lock.acquire()
        task = asyncio.create_task(provider.complete(supplied))
        try:
            await asyncio.sleep(0)
            assert not task.done()
            object.__setattr__(supplied, "model", "retargeted-model")
            object.__setattr__(
                supplied,
                "messages",
                (ModelMessage(role="user", content="retargeted"),),
            )
            object.__setattr__(supplied, "timeout_seconds", 0.000001)
        finally:
            provider._inference_lock.release()

        response = await task
        assert response.model == "test-model"
        assert manager.catalog.requested_aliases == ["test-model"]
        assert model.last_messages == [{"role": "user", "content": "hello"}]

    asyncio.run(scenario())


def test_foundry_local_inference_never_downloads_uncached_model() -> None:
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
    assert "explicit model download action" in str(exc_info.value)
    assert model.downloaded is False
    assert model.is_loaded is False


def test_foundry_provider_rejects_behavioral_legacy_download_flag() -> None:
    class BehavioralFlag:
        def __bool__(self) -> bool:
            raise AssertionError("legacy flag truthiness must not execute")

    with pytest.raises(TypeError, match="allow_download must be boolean"):
        FoundryLocalProvider(
            default_model="test-model",
            allow_download=BehavioralFlag(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "app_name",
    (
        "",
        " NikaCore",
        "NikaCore\n",
    ),
)
def test_foundry_provider_rejects_noncanonical_app_name(app_name: str) -> None:
    with pytest.raises(ValueError):
        FoundryLocalProvider(
            default_model="test-model",
            app_name=app_name,
        )


def test_foundry_provider_rejects_behavioral_app_name() -> None:
    class BehavioralText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("behavioral app_name methods must not execute")

    with pytest.raises(TypeError, match="app_name must be text"):
        FoundryLocalProvider(
            default_model="test-model",
            app_name=BehavioralText("NikaCore"),
        )


def test_legacy_provider_download_flag_is_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="download_model"):
        FoundryLocalProvider(default_model="test-model", allow_download=True)


def test_foundry_provider_rejects_behavioral_model_identity_carriers() -> None:
    class BehavioralText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("behavioral string methods must not execute")

    with pytest.raises(TypeError, match="default_model must be text"):
        FoundryLocalProvider(default_model=BehavioralText("test-model"))

    with pytest.raises(TypeError, match="expected_model_id must be text"):
        FoundryLocalProvider(
            default_model="test-model",
            expected_model_id=BehavioralText("test-model-id"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("default_model", "test\tmodel"),
        ("expected_model_id", "model\x00id"),
    ),
)
def test_foundry_provider_rejects_control_characters_in_model_identity(
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, object] = {"default_model": "test-model"}
    kwargs[field] = value
    with pytest.raises(ValueError, match="control characters"):
        FoundryLocalProvider(**kwargs)  # type: ignore[arg-type]


def test_foundry_inspect_rejects_explicit_empty_alias_without_default_fallback() -> None:
    model = FakeFoundryModel()
    manager = FakeManager(model)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ValueError, match="model_alias must not be empty"):
        provider.inspect_model("")

    assert manager.catalog.requested_aliases == []


def test_foundry_inspect_rejects_behavioral_alias_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid model_alias")

    class BehavioralText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("behavioral string methods must not execute")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )

    with pytest.raises(TypeError, match="model_alias must be text"):
        provider.inspect_model(BehavioralText("other-model"))

    assert calls == []


def test_explicit_download_action_requires_exact_authorization_then_allows_inference() -> None:
    model = FakeFoundryModel(cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )
    cancel_event = threading.Event()

    evidence = asyncio.run(
        provider.download_model(authorization(), cancel_event=cancel_event)
    )
    response = asyncio.run(provider.complete(request()))

    assert evidence.alias == "test-model"
    assert evidence.cached is True
    assert model.downloaded is True
    assert model.download_cancel_event is cancel_event
    assert response.text == "embedded: hello"
    assert model.is_loaded is True


def test_download_authorization_rejects_missing_license_reference() -> None:
    with pytest.raises(ValueError, match="license_reference"):
        authorization(license_reference=" ")


def test_download_authorization_rejects_non_text_authority_carriers() -> None:
    for field, value in (
        ("provider_id", 7),
        ("model", object()),
        ("license_reference", b"license"),
        ("expected_model_id", 9),
    ):
        with pytest.raises(TypeError, match=field):
            authorization(**{field: value})


def test_download_authorization_rejects_str_subclass_authority_carriers() -> None:
    class Text(str):
        pass

    for field, value in (
        ("provider_id", Text("foundry-local")),
        ("model", Text("test-model")),
        ("license_reference", Text("MODEL-LICENSE-REVIEW-123")),
        ("expected_model_id", Text("test-model-id")),
    ):
        with pytest.raises(TypeError, match=field):
            authorization(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "foundry-local\n"),
        ("model", "test\tmodel"),
        ("license_reference", "MODEL\x00LICENSE"),
        ("expected_model_id", "model-id\rvalue"),
    ],
)
def test_download_authorization_rejects_control_characters(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="control characters|surrounding whitespace"):
        authorization(**{field: value})


def test_download_authorization_accepts_canonical_expected_model_id() -> None:
    value = authorization(expected_model_id="test-model-id")

    assert value.expected_model_id == "test-model-id"


def test_foundry_download_revalidates_forged_authorization_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid authorization")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )
    forged = authorization()
    object.__setattr__(forged, "model", object())

    with pytest.raises(TypeError, match="model"):
        asyncio.run(provider.download_model(forged))

    assert calls == []


def test_foundry_download_snapshots_authorization_before_waiting() -> None:
    async def scenario() -> None:
        model = FakeFoundryModel()
        manager = FakeManager(model)
        provider = FoundryLocalProvider(
            default_model="test-model",
            manager_factory=lambda: manager,
        )
        supplied = authorization(expected_model_id="test-model-id")

        await provider._model_management_lock.acquire()
        task = asyncio.create_task(
            provider.download_model(supplied, timeout_seconds=1.0)
        )
        try:
            await asyncio.sleep(0)
            assert not task.done()
            object.__setattr__(supplied, "model", "mutated-model")
            object.__setattr__(supplied, "expected_model_id", "mutated-model-id")
        finally:
            provider._model_management_lock.release()

        evidence = await task
        assert evidence.alias == "test-model"
        assert evidence.model_id == "test-model-id"
        assert manager.catalog.requested_aliases == ["test-model"]

    asyncio.run(scenario())


def test_foundry_download_rejects_behavioral_cancel_event_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid cancel_event")

    class BehavioralEvent(threading.Event):
        def __bool__(self) -> bool:
            raise AssertionError("cancel_event truthiness must not execute")

        def is_set(self) -> bool:
            raise AssertionError("cancel_event subclass behavior must not execute")

        def set(self) -> None:
            raise AssertionError("cancel_event subclass behavior must not execute")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )

    with pytest.raises(TypeError, match="cancel_event must be exact threading.Event"):
        asyncio.run(
            provider.download_model(
                authorization(),
                cancel_event=BehavioralEvent(),
            )
        )

    assert calls == []


def test_foundry_download_rejects_non_event_cancel_carrier_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for invalid cancel_event")

    class BehavioralCarrier:
        def __bool__(self) -> bool:
            raise AssertionError("cancel_event truthiness must not execute")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )

    with pytest.raises(TypeError, match="cancel_event must be exact threading.Event"):
        asyncio.run(
            provider.download_model(
                authorization(),
                cancel_event=BehavioralCarrier(),  # type: ignore[arg-type]
            )
        )

    assert calls == []


def test_foundry_download_does_not_promote_success_after_external_cancel() -> None:
    async def scenario() -> None:
        model = BlockingDownloadModel()
        provider = FoundryLocalProvider(
            default_model="test-model",
            manager_factory=lambda: FakeManager(model),
        )
        cancel_event = threading.Event()
        task = asyncio.create_task(
            provider.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )

        started = await asyncio.to_thread(model.download_started.wait, 2.0)
        assert started is True
        cancel_event.set()
        model.release_download.set()

        with pytest.raises(ModelGatewayError) as exc_info:
            await task

        assert exc_info.value.code is ModelErrorCode.CANCELLED
        assert exc_info.value.retryable is False
        assert model.downloaded is True
        assert model.is_cached is True

    asyncio.run(scenario())


def test_foundry_cached_download_rechecks_cancel_after_evidence_collection() -> None:
    cancel_event = threading.Event()
    model = CancelDuringEvidenceModel(cancel_event, cached=True)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            provider.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )

    assert exc_info.value.code is ModelErrorCode.CANCELLED
    assert exc_info.value.retryable is False
    assert model.downloaded is False


def test_foundry_download_rechecks_cancel_after_post_download_evidence() -> None:
    cancel_event = threading.Event()
    model = CancelDuringEvidenceModel(cancel_event, cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            provider.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )

    assert exc_info.value.code is ModelErrorCode.CANCELLED
    assert exc_info.value.retryable is False
    assert model.downloaded is True
    assert model.is_cached is True


def test_foundry_download_honors_pre_set_cancel_before_manager() -> None:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached after cancellation")

    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=manager_factory,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(
            provider.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )

    assert exc_info.value.code is ModelErrorCode.CANCELLED
    assert exc_info.value.retryable is False
    assert calls == []


def test_foundry_download_rechecks_cancel_after_management_wait() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        def manager_factory() -> object:
            calls.append("manager")
            raise AssertionError("manager must not be reached after cancellation")

        provider = FoundryLocalProvider(
            default_model="test-model",
            manager_factory=manager_factory,
        )
        cancel_event = threading.Event()

        await provider._model_management_lock.acquire()
        task = asyncio.create_task(
            provider.download_model(
                authorization(),
                cancel_event=cancel_event,
                timeout_seconds=1.0,
            )
        )
        try:
            await asyncio.sleep(0)
            assert not task.done()
            cancel_event.set()
        finally:
            provider._model_management_lock.release()

        with pytest.raises(ModelGatewayError) as exc_info:
            await task

        assert exc_info.value.code is ModelErrorCode.CANCELLED
        assert exc_info.value.retryable is False
        assert calls == []

    asyncio.run(scenario())


def test_foundry_download_rejects_authorization_for_other_provider() -> None:
    model = FakeFoundryModel(cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ValueError, match="provider"):
        asyncio.run(provider.download_model(authorization(provider_id="other-local")))

    assert model.downloaded is False


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


def test_request_model_override_cannot_grant_download_permission() -> None:
    model = FakeFoundryModel(cached=False)
    provider = FoundryLocalProvider(
        default_model="small-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request(model="large-network-model")))

    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert model.downloaded is False


def test_foundry_local_maps_request_timeout_to_nonretryable_gateway_error() -> None:
    model = FakeFoundryModel(completion_delay=0.05)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request(timeout_seconds=0.01)))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is False


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


def test_timed_out_native_inference_keeps_slot_until_worker_finishes() -> None:
    model = FakeFoundryModel(completion_delay=0.05)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    async def scenario() -> None:
        with pytest.raises(ModelGatewayError) as first_error:
            await provider.complete(request(request_id="first", timeout_seconds=0.01))
        assert first_error.value.code is ModelErrorCode.TIMEOUT
        assert model.active_completions == 1

        with pytest.raises(ModelGatewayError) as second_error:
            await provider.complete(request(request_id="second", timeout_seconds=0.01))
        assert second_error.value.code is ModelErrorCode.TIMEOUT
        assert model.completion_count == 1
        assert model.max_active_completions == 1

        await asyncio.sleep(0.06)
        response = await provider.complete(request(request_id="third", timeout_seconds=1.0))
        assert response.request_id == "third"

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.max_active_completions == 1


def test_foundry_local_inspect_model_returns_read_only_sdk_evidence() -> None:
    model = FakeFoundryModel()
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: FakeManager(model),
    )

    evidence = provider.inspect_model()

    assert evidence.model_id == "test-model-id"
    assert evidence.alias == "test-model"
    assert evidence.cached is True
    assert evidence.loaded is False
    assert evidence.path == "C:/Nika Test Models/test-model"
    assert evidence.context_length == 4096
    assert evidence.input_modalities == "text"
    assert evidence.output_modalities == "text"
    assert evidence.capability_tags == "chat,completion"
    assert evidence.supports_tool_calling is False
    assert model.downloaded is False
    assert model.is_loaded is False


def test_foundry_local_missing_catalog_model_is_typed_unavailable() -> None:
    provider = FoundryLocalProvider(
        default_model="missing-model",
        manager_factory=lambda: FakeManager(None),
    )

    with pytest.raises(ModelGatewayError) as inspect_error:
        provider.inspect_model()
    assert inspect_error.value.code is ModelErrorCode.UNAVAILABLE

    with pytest.raises(ModelGatewayError) as completion_error:
        asyncio.run(provider.complete(request(model="missing-model")))
    assert completion_error.value.code is ModelErrorCode.UNAVAILABLE

    with pytest.raises(ModelGatewayError) as download_error:
        asyncio.run(provider.download_model(authorization(model="missing-model")))
    assert download_error.value.code is ModelErrorCode.UNAVAILABLE
