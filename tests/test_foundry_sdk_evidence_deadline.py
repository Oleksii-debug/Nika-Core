from __future__ import annotations

import asyncio
import time
from threading import Event

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Model:
    def __init__(self, *, cached: bool, evidence_delay: float = 0.0) -> None:
        self.id = "fixture-variant"
        self.alias = "fixture-model"
        self.is_cached = cached
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.evidence_delay = evidence_delay
        self.downloads = 0

    def get_path(self) -> str:
        if self.evidence_delay:
            time.sleep(self.evidence_delay)
        return "C:/Foundry Models/fixture-model"

    def download(self, *, cancel_event: Event) -> None:
        self.downloads += 1
        self.is_cached = True


class _Catalog:
    def __init__(self, model: _Model, *, delay: float = 0.0) -> None:
        self.model = model
        self.delay = delay
        self.calls = 0

    def get_model(self, alias: str) -> _Model:
        self.calls += 1
        assert alias == "fixture-model"
        if self.delay:
            time.sleep(self.delay)
        return self.model


class _Manager:
    def __init__(self, catalog: _Catalog) -> None:
        self.catalog = catalog


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="fixture-model",
        license_reference="TEST-LICENSE-EVIDENCE",
    )


@pytest.mark.parametrize("stage", ("catalog", "cached-evidence", "download-evidence"))
def test_blocking_sdk_evidence_cannot_publish_success_after_deadline(stage: str) -> None:
    model = _Model(
        cached=stage != "download-evidence",
        evidence_delay=0.30 if stage != "catalog" else 0.0,
    )
    catalog = _Catalog(model, delay=0.30 if stage == "catalog" else 0.0)
    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: _Manager(catalog),
    )
    cancellation = Event()

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            provider.download_model(
                _authorization(),
                cancel_event=cancellation,
                timeout_seconds=0.20,
            )
        )

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert cancellation.is_set()
    assert catalog.calls == 1
    assert model.downloads == (1 if stage == "download-evidence" else 0)
    assert not provider._inference_lock.locked()
    assert not provider._model_management_lock.locked()


@pytest.mark.parametrize("cached", (True, False))
def test_in_budget_cached_or_downloaded_evidence_still_succeeds(cached: bool) -> None:
    model = _Model(cached=cached)
    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: _Manager(_Catalog(model)),
    )
    cancellation = Event()

    evidence = asyncio.run(
        provider.download_model(
            _authorization(),
            cancel_event=cancellation,
            timeout_seconds=2.0,
        )
    )

    assert evidence.model_id == "fixture-variant"
    assert evidence.alias == "fixture-model"
    assert evidence.cached is True
    assert evidence.path == "C:/Foundry Models/fixture-model"
    assert model.downloads == (0 if cached else 1)
    assert not cancellation.is_set()
