from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class SlowDownloadModel:
    def __init__(self) -> None:
        self.id = "slow-model-id"
        self.alias = "slow-model"
        self.is_cached = False
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.download_started = threading.Event()
        self.download_active = False
        self.download_cancel_event: threading.Event | None = None
        self.overlap_detected = False

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_cancel_event = cancel_event
        self.download_active = True
        self.download_started.set()
        try:
            # Deliberately simulate a native worker that notices cancellation slowly.
            time.sleep(0.05)
            self.is_cached = True
        finally:
            self.download_active = False

    def get_path(self) -> str:
        return "C:/Nika Test Models/slow-model"

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                if model.download_active:
                    model.overlap_detected = True
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                    usage=None,
                )

        return Client()


class Catalog:
    def __init__(self, model: SlowDownloadModel) -> None:
        self.model = model

    def get_model(self, alias: str) -> SlowDownloadModel | None:
        return self.model if alias == self.model.alias else None

    def get_loaded_models(self) -> list[SlowDownloadModel]:
        return [self.model] if self.model.is_loaded else []


class Manager:
    def __init__(self, model: SlowDownloadModel) -> None:
        self.catalog = Catalog(model)


def test_cancelled_model_download_retains_slot_until_native_worker_finishes() -> None:
    model = SlowDownloadModel()
    provider = FoundryLocalProvider(
        default_model="slow-model",
        manager_factory=lambda: Manager(model),
    )
    authorization = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="slow-model",
        license_reference="MODEL-LICENSE-REVIEW-SLOW",
    )
    request = ModelRequest(
        request_id="after-cancelled-download",
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=1.0,
    )

    async def scenario() -> None:
        download_task = asyncio.create_task(provider.download_model(authorization))
        started = await asyncio.to_thread(model.download_started.wait, 1.0)
        assert started is True

        download_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await download_task

        response = await provider.complete(request)
        assert response.text == "ready"

    asyncio.run(scenario())

    assert model.download_cancel_event is not None
    assert model.download_cancel_event.is_set() is True
    assert model.overlap_detected is False
