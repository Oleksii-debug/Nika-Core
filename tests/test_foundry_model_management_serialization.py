from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class EarlyCacheModel:
    id = "early-cache-cpu:1"
    alias = "early-cache"
    context_length = 1024
    input_modalities = "text"
    output_modalities = "text"
    capabilities = "chat"
    supports_tool_calling = False
    is_loaded = False

    def __init__(self) -> None:
        self.is_cached = False
        self.download_count = 0
        self.download_started = threading.Event()
        self.release_download = threading.Event()
        self.cancel_event: threading.Event | None = None

    def download(self, *, cancel_event: threading.Event | None = None) -> None:
        self.download_count += 1
        self.cancel_event = cancel_event
        # Simulate an SDK that exposes cache presence before all native download
        # cleanup/finalization has returned to the caller.
        self.is_cached = True
        self.download_started.set()
        if not self.release_download.wait(timeout=5.0):
            raise RuntimeError("test download worker was not released")

    def get_path(self) -> str:
        return "C:/Nika Test Models/early-cache"


class Manager:
    def __init__(self, model: EarlyCacheModel) -> None:
        self.catalog = SimpleNamespace(get_model=lambda alias: model if alias == model.alias else None)


def authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="early-cache",
        license_reference="MODEL-LICENSE-REVIEW",
        expected_model_id="early-cache-cpu:1",
    )


def test_timed_out_download_does_not_publish_cached_success_before_native_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = EarlyCacheModel()
    provider = FoundryLocalProvider(
        default_model="early-cache",
        manager_factory=lambda: Manager(model),
    )

    async def scenario() -> None:
        original_wait_for = asyncio.wait_for
        forced_worker_timeout = False

        async def controlled_wait_for(awaitable: object, timeout: float | None) -> object:
            nonlocal forced_worker_timeout
            if not forced_worker_timeout and isinstance(awaitable, asyncio.Future):
                forced_worker_timeout = True
                await asyncio.sleep(0)
                raise TimeoutError
            return await original_wait_for(awaitable, timeout=timeout)  # type: ignore[arg-type]

        monkeypatch.setattr(asyncio, "wait_for", controlled_wait_for)

        with pytest.raises(ModelGatewayError) as first_error:
            await provider.download_model(authorization(), timeout_seconds=1.0)
        assert first_error.value.code is ModelErrorCode.TIMEOUT
        assert forced_worker_timeout is True

        started = await asyncio.to_thread(model.download_started.wait, 1.0)
        assert started is True
        assert model.is_cached is True
        assert model.cancel_event is not None and model.cancel_event.is_set()

        # Even though the fake SDK already reports cached=True, a second model
        # management action must not publish success until the original native
        # worker has actually exited and released the management slot.
        with pytest.raises(ModelGatewayError) as second_error:
            await provider.download_model(authorization(), timeout_seconds=0.01)
        assert second_error.value.code is ModelErrorCode.TIMEOUT

        # Release the native worker explicitly. The next bounded management action
        # itself becomes the synchronization proof for slot ownership/release.
        model.release_download.set()
        evidence = await provider.download_model(authorization(), timeout_seconds=1.0)
        assert evidence.cached is True
        assert model.download_count == 1

    asyncio.run(scenario())
