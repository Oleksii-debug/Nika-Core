from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import ModelDownloadAuthorization
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Model:
    def __init__(self) -> None:
        self.id = "original-model-id"
        self.alias = "original-model"
        self.is_cached = False
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.download_calls = 0

    def download(self, *, cancel_event: Event | None = None) -> None:
        assert cancel_event is not None
        self.download_calls += 1
        self.is_cached = True

    def get_path(self) -> str:
        return "C:/Nika Test Models/original-model"


class _Catalog:
    def __init__(self, model: _Model) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> _Model | None:
        self.requested_aliases.append(alias)
        if alias != "original-model":
            return None
        return self.model


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="original-model",
        license_reference="MODEL-LICENSE-REVIEW-SNAPSHOT",
        expected_model_id="original-model-id",
    )


def test_download_rejects_authorization_subclass_before_manager_access() -> None:
    class ForgedAuthorization(ModelDownloadAuthorization):
        pass

    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached")

    provider = FoundryLocalProvider(
        default_model="original-model",
        manager_factory=manager_factory,
    )
    forged = ForgedAuthorization(
        provider_id="foundry-local",
        model="original-model",
        license_reference="MODEL-LICENSE-REVIEW-SNAPSHOT",
        expected_model_id="original-model-id",
    )

    with pytest.raises(TypeError, match="ModelDownloadAuthorization"):
        asyncio.run(provider.download_model(forged))

    assert calls == []


def test_download_uses_pre_await_authorization_snapshot() -> None:
    async def scenario() -> None:
        model = _Model()
        catalog = _Catalog(model)
        manager = SimpleNamespace(catalog=catalog)
        provider = FoundryLocalProvider(
            default_model="original-model",
            manager_factory=lambda: manager,
        )
        authorization = _authorization()

        await provider._model_management_lock.acquire()
        task = asyncio.create_task(
            provider.download_model(authorization, timeout_seconds=1.0)
        )
        try:
            await asyncio.sleep(0)
            object.__setattr__(authorization, "model", "mutated-model")
            object.__setattr__(
                authorization,
                "expected_model_id",
                "mutated-model-id",
            )
        finally:
            provider._model_management_lock.release()

        evidence = await task

        assert catalog.requested_aliases == ["original-model"]
        assert evidence.model_id == "original-model-id"
        assert evidence.alias == "original-model"
        assert model.download_calls == 1

    asyncio.run(scenario())
