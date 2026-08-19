from __future__ import annotations

import asyncio
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
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.resources.contracts import ResourceSnapshot


class EvidenceModel:
    def __init__(
        self,
        *,
        model_id: str = "test-model-cpu:7",
        alias: str = "test-model",
        cached: bool = True,
        loaded: bool = False,
        completion_delay: float = 0.0,
        download_delay: float = 0.0,
    ) -> None:
        self.id = model_id
        self.alias = alias
        self.is_cached = cached
        self.is_loaded = loaded
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.completion_delay = completion_delay
        self.download_delay = download_delay
        self.load_count = 0
        self.unload_count = 0
        self.completion_count = 0
        self.download_count = 0
        self.download_started = threading.Event()
        self.download_cancel_event: threading.Event | None = None

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_count += 1
        self.download_cancel_event = cancel_event
        self.download_started.set()
        if self.download_delay:
            time.sleep(self.download_delay)
        self.is_cached = True

    def get_path(self) -> str:
        return "C:/Nika Test Models/test-model"

    def load(self) -> None:
        self.load_count += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_count += 1
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                model.completion_count += 1
                if model.completion_delay:
                    time.sleep(model.completion_delay)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                    usage=SimpleNamespace(
                        prompt_tokens=2,
                        completion_tokens=1,
                        total_tokens=3,
                    ),
                )

        return Client()


class Catalog:
    def __init__(self, model: EvidenceModel | None) -> None:
        self.model = model

    def get_model(self, alias: str) -> EvidenceModel | None:
        if self.model is None:
            return None
        if alias != self.model.alias:
            return None
        return self.model

    def get_loaded_models(self) -> list[EvidenceModel]:
        if self.model is None or not self.model.is_loaded:
            return []
        return [self.model]


class Manager:
    def __init__(self, model: EvidenceModel | None) -> None:
        self.catalog = Catalog(model)


class StaticObserver:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> ResourceSnapshot:
        self.calls += 1
        return self._snapshot


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "foundry-evidence-test",
        "messages": (ModelMessage(role="user", content="hello"),),
        "provider_id": "foundry-local",
        "privacy": PrivacyClass.SENSITIVE,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def authorization(**overrides: object) -> ModelDownloadAuthorization:
    values: dict[str, object] = {
        "provider_id": "foundry-local",
        "model": "test-model",
        "license_reference": "MODEL-LICENSE-REVIEW-EXACT",
        "expected_model_id": "test-model-cpu:7",
    }
    values.update(overrides)
    return ModelDownloadAuthorization(**values)  # type: ignore[arg-type]


def test_public_variant_identity_and_version_are_exposed_without_loading() -> None:
    model = EvidenceModel()
    provider = FoundryLocalProvider(
        default_model="test-model",
        expected_model_id="test-model-cpu:7",
        manager_factory=lambda: Manager(model),
    )

    evidence = provider.inspect_model()

    assert evidence.model_id == "test-model-cpu:7"
    assert evidence.model_version == "7"
    assert evidence.alias == "test-model"
    assert model.load_count == 0
    assert model.completion_count == 0


def test_provider_pin_rejects_alias_that_resolves_to_another_variant() -> None:
    model = EvidenceModel(model_id="test-model-cpu:8")
    provider = FoundryLocalProvider(
        default_model="test-model",
        expected_model_id="test-model-cpu:7",
        manager_factory=lambda: Manager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        provider.inspect_model()

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert model.load_count == 0


def test_download_authorization_pin_rejects_wrong_variant_before_download() -> None:
    model = EvidenceModel(model_id="test-model-cpu:8", cached=False)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: Manager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.download_model(authorization()))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert model.download_count == 0


def test_resource_policy_blocks_inference_before_model_load() -> None:
    model = EvidenceModel()
    observer = StaticObserver(
        ResourceSnapshot(
            cpu_percent=92.0,
            memory_percent=55.0,
            available_memory_bytes=8 * 1024**3,
        )
    )
    provider = FoundryLocalProvider(
        default_model="test-model",
        resource_policy=ModelResourcePolicy(max_cpu_percent=80.0),
        resource_observer=observer,
        manager_factory=lambda: Manager(model),
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(request()))

    assert exc_info.value.code is ModelErrorCode.RESOURCE_LIMIT
    assert exc_info.value.retryable is False
    assert observer.calls == 1
    assert model.load_count == 0
    assert model.completion_count == 0


def test_resource_policy_requires_observer_and_valid_budget() -> None:
    with pytest.raises(ValueError, match="resource_observer"):
        FoundryLocalProvider(
            default_model="test-model",
            resource_policy=ModelResourcePolicy(max_memory_percent=80.0),
        )

    with pytest.raises(ValueError, match="max_cpu_percent"):
        ModelResourcePolicy(max_cpu_percent=101.0)

    with pytest.raises(ValueError, match="min_available_memory_bytes"):
        ModelResourcePolicy(min_available_memory_bytes=0)


def test_close_does_not_unload_model_that_provider_did_not_load() -> None:
    model = EvidenceModel(loaded=True)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: Manager(model),
    )

    response = asyncio.run(provider.complete(request()))
    provider.close()

    assert response.text == "ready"
    assert model.load_count == 0
    assert model.unload_count == 0
    assert model.is_loaded is True


def test_close_unloads_only_provider_owned_model_and_allows_reload() -> None:
    model = EvidenceModel()
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: Manager(model),
    )

    first = asyncio.run(provider.complete(request(request_id="first")))
    provider.close()
    second = asyncio.run(provider.complete(request(request_id="second")))
    provider.close()

    assert first.text == "ready"
    assert second.text == "ready"
    assert model.load_count == 2
    assert model.unload_count == 2
    assert model.is_loaded is False


def test_close_refuses_to_race_timed_out_native_inference() -> None:
    model = EvidenceModel(completion_delay=0.05)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: Manager(model),
    )

    async def scenario() -> None:
        with pytest.raises(ModelGatewayError) as exc_info:
            await provider.complete(request(timeout_seconds=0.01))
        assert exc_info.value.code is ModelErrorCode.TIMEOUT

        with pytest.raises(RuntimeError, match="native work is active"):
            provider.close()

        # Do not guess how quickly a platform will schedule the to_thread completion
        # callback. A normal follow-up inference must wait for the retained slot,
        # then prove the provider is reusable only after the native worker exits.
        after_timeout = await provider.complete(
            request(request_id="after-timeout", timeout_seconds=1.0)
        )
        assert after_timeout.text == "ready"
        provider.close()

    asyncio.run(scenario())

    assert model.completion_count == 2
    assert model.unload_count == 1


def test_download_timeout_signals_cancel_and_retains_slot_until_worker_exits() -> None:
    model = EvidenceModel(cached=False, download_delay=0.05)
    provider = FoundryLocalProvider(
        default_model="test-model",
        manager_factory=lambda: Manager(model),
    )

    async def scenario() -> None:
        with pytest.raises(ModelGatewayError) as download_error:
            await provider.download_model(authorization(), timeout_seconds=0.01)
        assert download_error.value.code is ModelErrorCode.TIMEOUT
        assert model.download_cancel_event is not None
        assert model.download_cancel_event.is_set() is True

        with pytest.raises(ModelGatewayError) as inference_error:
            await provider.complete(request(request_id="blocked", timeout_seconds=0.01))
        assert inference_error.value.code is ModelErrorCode.TIMEOUT
        assert model.completion_count == 0

        # A second management operation with a real deadline waits for the retained
        # native-download ownership and can publish cached evidence only after exit.
        cached = await provider.download_model(authorization(), timeout_seconds=1.0)
        assert cached.cached is True

        response = await provider.complete(request(request_id="after-download"))
        assert response.text == "ready"
        provider.close()

    asyncio.run(scenario())

    assert model.download_count == 1
    assert model.completion_count == 1
    assert model.unload_count == 1


def test_authorization_identity_fields_reject_ambiguous_whitespace() -> None:
    with pytest.raises(ValueError, match="license_reference"):
        authorization(license_reference=" reviewed ")
    with pytest.raises(ValueError, match="expected_model_id"):
        authorization(expected_model_id=" test-model-cpu:7 ")
