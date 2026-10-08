"""The real SDK cancellation signal is caller-owned and fail-closed."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


def _permit() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="safe-model",
        license_reference="REVIEWED-LICENSE",
    )


def test_behavioral_falsy_event_cannot_disable_cancel_signal() -> None:
    events: list[str] = []

    class HostileEvent(Event):
        def __bool__(self) -> bool:
            events.append("truthy")
            raise AssertionError("client event executed arbitrary __bool__")

    provider = FoundryLocalProvider(
        default_model="safe-model",
        manager_factory=lambda: (_ for _ in ()).throw(AssertionError("SDK reached")),
    )
    with pytest.raises(ValueError, match="cancel_event"):
        asyncio.run(provider.download_model(_permit(), cancel_event=HostileEvent()))
    assert events == []
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()


def test_pre_cancelled_download_never_touches_sdk_or_locks() -> None:
    sdk_calls: list[str] = []

    def forbidden_manager() -> object:
        sdk_calls.append("called")
        raise AssertionError("cancelled request reached the Foundry SDK")

    cancelled = Event()
    cancelled.set()
    provider = FoundryLocalProvider(
        default_model="safe-model", manager_factory=forbidden_manager
    )
    with pytest.raises(ModelGatewayError) as error:
        asyncio.run(provider.download_model(_permit(), cancel_event=cancelled))
    assert error.value.code is ModelErrorCode.CANCELLED
    assert sdk_calls == []
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()


def test_cancel_during_management_lock_wait_fails_closed_without_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[str] = []
    provider = FoundryLocalProvider(default_model="safe-model")
    monkeypatch.setattr(
        provider, "_get_model",
        lambda alias: lookups.append(alias),
    )
    cancelled = Event()

    async def exercise() -> None:
        await provider._model_management_lock.acquire()
        pending = asyncio.create_task(
            provider.download_model(_permit(), cancel_event=cancelled, timeout_seconds=1)
        )
        try:
            await asyncio.sleep(0.01)
            cancelled.set()
        finally:
            provider._model_management_lock.release()
        with pytest.raises(ModelGatewayError) as error:
            await pending
        assert error.value.code is ModelErrorCode.CANCELLED

    asyncio.run(exercise())
    assert lookups == []
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()


def test_uncancelled_plain_event_preserves_cached_model_path() -> None:
    event = Event()
    lookups: list[str] = []
    model = SimpleNamespace(
        id="safe-model-v1",
        alias="safe-model",
        is_cached=True,
        is_loaded=False,
        get_path=lambda: "test-cache",
    )

    class Manager:
        def __init__(self) -> None:
            self.catalog = SimpleNamespace(
                get_model=lambda alias: (lookups.append(alias), model)[1]
            )

    provider = FoundryLocalProvider(
        default_model="safe-model", manager_factory=Manager,
    )
    evidence = asyncio.run(provider.download_model(_permit(), cancel_event=event))
    assert evidence.cached
    assert evidence.model_id == "safe-model-v1"
    assert lookups == ["safe-model"]
    assert not event.is_set()
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()
