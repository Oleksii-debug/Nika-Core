from __future__ import annotations

import asyncio
import time
from threading import Event
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _SlowOrCancellingModel:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.provider: FoundryLocalProvider | None = None
        self.alias = "approved-alias"
        self.is_cached = False
        self.is_loaded = False
        self.download_calls = 0
        self.pre_download_getter_seen = False
        self.cancellation = Event()

    @property
    def id(self) -> str:
        provider = self.provider
        if (
            provider is not None
            and provider._inference_lock.locked()
            and not self.pre_download_getter_seen
        ):
            self.pre_download_getter_seen = True
            if self.stage == "expired":
                # The event loop is blocked in this synchronous SDK getter.
                time.sleep(0.08)
            elif self.stage == "cancelled":
                self.cancellation.set()
        return "approved-artifact:1"

    def download(self, *, cancel_event: Event) -> None:
        self.download_calls += 1
        assert not cancel_event.is_set()
        self.is_cached = True

    def get_path(self) -> str:
        return "C:/Foundry Models/approved-alias"


class _SlowOrCancellingAliasModel(_SlowOrCancellingModel):
    @property
    def alias(self) -> str:
        provider = self.provider
        if (
            provider is not None
            and provider._inference_lock.locked()
            and not self.pre_download_getter_seen
        ):
            self.pre_download_getter_seen = True
            if self.stage == "expired":
                time.sleep(0.08)
            elif self.stage == "cancelled":
                self.cancellation.set()
        return self._alias

    @alias.setter
    def alias(self, value: str) -> None:
        self._alias = value


def _provider(model: _SlowOrCancellingModel) -> FoundryLocalProvider:
    manager = SimpleNamespace(
        catalog=SimpleNamespace(get_model=lambda alias: model),
    )
    provider = FoundryLocalProvider(
        default_model="approved-alias",
        manager_factory=lambda: manager,
    )
    model.provider = provider
    return provider


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="approved-alias",
        license_reference="qa-reviewed-license",
    )


@pytest.mark.parametrize("getter", ("id", "alias"))
@pytest.mark.parametrize(
    ("stage", "expected_code"),
    (
        ("expired", ModelErrorCode.TIMEOUT),
        ("cancelled", ModelErrorCode.CANCELLED),
    ),
)
def test_sdk_getter_cannot_start_download_after_abandonment(
    getter: str, stage: str, expected_code: ModelErrorCode
) -> None:
    model = (
        _SlowOrCancellingModel(stage)
        if getter == "id"
        else _SlowOrCancellingAliasModel(stage)
    )
    provider = _provider(model)
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(
            provider.download_model(
                _authorization(),
                cancel_event=model.cancellation,
                timeout_seconds=0.03 if stage == "expired" else 2.0,
            )
        )
    assert model.pre_download_getter_seen
    assert caught.value.code is expected_code
    assert caught.value.retryable is False
    assert model.cancellation.is_set()
    assert model.download_calls == 0
    assert not provider._inference_lock.locked()
    assert not provider._model_management_lock.locked()


def test_sdk_identity_getter_in_budget_preserves_explicit_download() -> None:
    model = _SlowOrCancellingModel("stable")
    provider = _provider(model)
    evidence = asyncio.run(
        provider.download_model(
            _authorization(),
            cancel_event=model.cancellation,
            timeout_seconds=2.0,
        )
    )
    assert model.pre_download_getter_seen
    assert model.download_calls == 1
    assert evidence.model_id == "approved-artifact:1"
    assert evidence.cached is True
    assert evidence.path == "C:/Foundry Models/approved-alias"
    assert not model.cancellation.is_set()
    assert not provider._inference_lock.locked()
    assert not provider._model_management_lock.locked()
