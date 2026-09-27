from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nika_core.intelligence.modes import IntelligenceMode, IntelligenceModeRouter
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway


class FakeFoundryModel:
    def __init__(
        self,
        *,
        alias: str = "fixture-model",
        cached: bool,
        load_error: Exception | None = None,
    ) -> None:
        self.id = "fixture-model-id"
        self.alias = alias
        self.is_cached = cached
        self.is_loaded = False
        self.load_error = load_error
        self.downloaded = False
        self.completion_started = False

    def download(self, **_: object) -> None:
        self.downloaded = True
        self.is_cached = True

    def load(self) -> None:
        if self.load_error is not None:
            raise self.load_error
        self.is_loaded = True

    def get_chat_client(self) -> object:
        model = self

        class Client:
            def complete_chat(self, messages: object) -> object:
                del messages
                model.completion_started = True
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="unexpected"))],
                    usage=None,
                )

        return Client()


class FakeCatalog:
    def __init__(self, model: FakeFoundryModel | None) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> FakeFoundryModel | None:
        self.requested_aliases.append(alias)
        if self.model is not None:
            self.model.alias = alias
        return self.model


class FakeManager:
    def __init__(self, model: FakeFoundryModel | None) -> None:
        self.catalog = FakeCatalog(model)


class RecordingCloudProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="cloud-fallback",
            kind=ProviderKind.CLOUD,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="cloud fallback must never run",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "cloud-default",
        )


def _request(model: str = "fixture-model") -> ModelRequest:
    return ModelRequest(
        request_id="embedded-missing-model-state",
        messages=(ModelMessage(role="user", content="hello"),),
        model=model,
        provider_id="cloud-fallback",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("cloud-fallback",),
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=5.0,
    )


def _run_embedded(
    model: FakeFoundryModel | None,
) -> tuple[ModelGatewayError, RecordingCloudProvider, FakeManager]:
    manager = FakeManager(model)
    foundry = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: manager,
    )
    cloud = RecordingCloudProvider()
    gateway = ModelGateway()
    gateway.register(foundry)
    gateway.register(cloud)
    router = IntelligenceModeRouter(gateway=gateway)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EMBEDDED_LOCAL, _request()))

    return caught.value, cloud, manager


def _scalar(value: Any) -> Any:
    return getattr(value, "value", value)


def _assert_actionable_state(
    error: ModelGatewayError,
    *,
    expected_state: str,
    expected_action: str,
    acquisition_allowed: bool,
) -> None:
    state = getattr(error, "model_state", None)
    assert state is not None, (
        "Embedded model failure must expose a structured model_state rather than "
        "only a generic ModelGatewayError message"
    )
    assert getattr(state, "model", None) == "fixture-model"
    assert getattr(state, "provider_id", None) == "foundry-local"
    assert _scalar(getattr(state, "state", None)) == expected_state
    assert _scalar(getattr(state, "action", None)) == expected_action
    assert getattr(state, "acquisition_allowed", None) is acquisition_allowed
    assert getattr(state, "manual_mode_selection_allowed", None) is True


def test_uncached_embedded_model_requires_explicit_acquisition_without_fallback() -> None:
    model = FakeFoundryModel(cached=False)

    error, cloud, manager = _run_embedded(model)

    assert error.code is ModelErrorCode.UNAVAILABLE
    assert cloud.calls == 0
    assert manager.catalog.requested_aliases == ["fixture-model"]
    assert model.downloaded is False
    assert model.is_loaded is False
    assert model.completion_started is False
    _assert_actionable_state(
        error,
        expected_state="missing",
        expected_action="acquire_model",
        acquisition_allowed=True,
    )


def test_cached_but_load_failing_embedded_model_is_actionable_not_generic() -> None:
    model = FakeFoundryModel(cached=True, load_error=OSError("fixture load failure"))

    error, cloud, manager = _run_embedded(model)

    assert error.code in {ModelErrorCode.UNAVAILABLE, ModelErrorCode.PROVIDER_ERROR}
    assert cloud.calls == 0
    assert manager.catalog.requested_aliases == ["fixture-model"]
    assert model.downloaded is False
    assert model.is_loaded is False
    assert model.completion_started is False
    _assert_actionable_state(
        error,
        expected_state="corrupt",
        expected_action="select_other_mode",
        acquisition_allowed=False,
    )


def test_catalog_unavailable_embedded_model_names_model_and_manual_alternative() -> None:
    error, cloud, manager = _run_embedded(None)

    assert error.code is ModelErrorCode.UNAVAILABLE
    assert cloud.calls == 0
    assert manager.catalog.requested_aliases == ["fixture-model"]
    _assert_actionable_state(
        error,
        expected_state="unavailable",
        expected_action="select_other_mode",
        acquisition_allowed=False,
    )
