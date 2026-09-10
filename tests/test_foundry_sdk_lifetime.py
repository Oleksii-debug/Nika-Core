from __future__ import annotations

import asyncio
import sys
from threading import Event
from types import SimpleNamespace

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _LifecycleModel:
    def __init__(self) -> None:
        self.id = "lifecycle-model-cpu:1"
        self.alias = "lifecycle-model"
        self.is_cached = True
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.load_count = 0
        self.unload_count = 0
        self.block_chat = False
        self.chat_started = Event()
        self.chat_release = Event()

    def get_path(self) -> str:
        return "C:/Nika Test Models/lifecycle-model"

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
                del messages
                if model.block_chat:
                    model.chat_started.set()
                    model.chat_release.wait()
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                    ),
                )

        return Client()


class _Catalog:
    def __init__(self, model: _LifecycleModel) -> None:
        self._model = model

    def get_model(self, alias: str) -> _LifecycleModel | None:
        return self._model if alias == self._model.alias else None


class _Manager:
    def __init__(self, model: _LifecycleModel) -> None:
        self.catalog = _Catalog(model)


def _provider(*, manager_factory: object | None = None) -> FoundryLocalProvider:
    kwargs: dict[str, object] = {"default_model": "lifecycle-model"}
    if manager_factory is not None:
        kwargs["manager_factory"] = manager_factory
    return FoundryLocalProvider(**kwargs)  # type: ignore[arg-type]


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="lifecycle probe"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=2.0,
    )


def _install_fake_sdk(monkeypatch: object) -> tuple[list[str], _LifecycleModel]:
    model = _LifecycleModel()
    manager = _Manager(model)
    events: list[str] = []

    class Configuration:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FoundryLocalManager:
        instance = manager

        @classmethod
        def initialize(cls, configuration: Configuration) -> None:
            assert configuration.kwargs["app_name"] == "NikaCore"
            events.append("initialize")

    fake_sdk = SimpleNamespace(
        Configuration=Configuration,
        FoundryLocalManager=FoundryLocalManager,
    )
    monkeypatch.setitem(sys.modules, "foundry_local_sdk", fake_sdk)  # type: ignore[attr-defined]
    return events, model


def test_multiple_providers_share_one_process_sdk_initialization(monkeypatch: object) -> None:
    events, _model = _install_fake_sdk(monkeypatch)
    first = _provider()
    second = _provider()

    assert first.inspect_model().model_id == "lifecycle-model-cpu:1"
    assert second.inspect_model().model_id == "lifecycle-model-cpu:1"

    assert events == ["initialize"]


def test_adapter_close_then_recreate_does_not_double_initialize_sdk(monkeypatch: object) -> None:
    events, _model = _install_fake_sdk(monkeypatch)
    first = _provider()

    assert first.inspect_model().cached is True
    first.close()

    replacement = _provider()
    assert replacement.inspect_model().cached is True

    # Provider.close() is not process shutdown. Recreating an adapter must reuse
    # the live process SDK generation rather than calling initialize() again.
    assert events == ["initialize"]


def test_owner_close_never_unloads_during_other_provider_native_request() -> None:
    model = _LifecycleModel()
    manager = _Manager(model)
    manager_factory = lambda: manager
    owner = _provider(manager_factory=manager_factory)
    consumer = _provider(manager_factory=manager_factory)

    async def scenario() -> None:
        assert (await owner.complete(_request("owner-prime"))).text == "ready"
        assert model.load_count == 1

        model.block_chat = True
        model.chat_started.clear()
        model.chat_release.clear()
        active = asyncio.create_task(consumer.complete(_request("consumer-active")))
        started = await asyncio.to_thread(model.chat_started.wait, 1.0)
        assert started is True

        try:
            try:
                owner.close()
            except RuntimeError:
                pass
            assert model.unload_count == 0
            assert model.is_loaded is True
        finally:
            model.chat_release.set()

        assert (await active).text == "ready"
        owner.close()

    asyncio.run(scenario())

    assert model.unload_count == 1
    assert model.is_loaded is False
