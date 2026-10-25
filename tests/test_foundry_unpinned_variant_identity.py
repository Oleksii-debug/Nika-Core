from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Model:
    def __init__(self, phase: str = "stable") -> None:
        self.phase = phase
        self.id = "stable-artifact:1"
        self.alias = "approved-alias"
        self._cached = phase not in ("download", "pre_download")
        self._loaded = phase in ("client", "chat", "usage")
        self.context_length = None
        self.input_modalities = None
        self.output_modalities = None
        self.capabilities = None
        self.supports_tool_calling = None
        self.chat_calls = 0
        self.load_calls = 0
        self.download_calls = 0
        self.unload_calls = 0

    @property
    def is_cached(self) -> bool:
        if self.phase in ("evidence", "cached", "pre_download"):
            self.id = "switched-artifact:2"
        return self._cached

    @property
    def is_loaded(self) -> bool:
        if self.phase == "loaded_status":
            self.id = "switched-artifact:2"
        return self._loaded

    @is_loaded.setter
    def is_loaded(self, value: bool) -> None:
        self._loaded = value

    def get_path(self) -> str:
        return "C:/Nika QA Models/approved-alias"

    def load(self) -> None:
        self.load_calls += 1
        self.is_loaded = True
        if self.phase == "load":
            self.id = "switched-artifact:2"

    def unload(self) -> None:
        self.unload_calls += 1
        self.is_loaded = False

    def download(self, *, cancel_event: object) -> None:
        del cancel_event
        self.download_calls += 1
        self._cached = True
        if self.phase == "download":
            self.id = "switched-artifact:2"

    def get_chat_client(self) -> object:
        model = self
        if self.phase == "client":
            self.id = "switched-artifact:2"

        class _Client:
            def complete_chat(self, messages: object) -> object:
                del messages
                model.chat_calls += 1
                if model.phase == "chat":
                    model.id = "switched-artifact:2"
                response = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="готово"))],
                    usage=None,
                )
                if model.phase == "usage":
                    class _Usage:
                        @property
                        def prompt_tokens(self) -> int:
                            model.id = "switched-artifact:2"
                            return 1

                        completion_tokens = 1
                        total_tokens = 2

                    response.usage = _Usage()
                return response

        return _Client()


def _provider(model: _Model) -> FoundryLocalProvider:
    manager = SimpleNamespace(catalog=SimpleNamespace(get_model=lambda alias: model))
    return FoundryLocalProvider(
        default_model="approved-alias",
        manager_factory=lambda: manager,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="unpinned-variant",
        messages=(ModelMessage(role="user", content="Привіт"),),
        model="approved-alias",
        timeout_seconds=2.0,
    )


def _assert_identity_failure(error: ModelGatewayError) -> None:
    assert error.code is ModelErrorCode.INVALID_REQUEST
    assert error.provider_id == "foundry-local"
    assert error.retryable is False


@pytest.mark.parametrize(
    ("phase", "chat_calls"),
    (
        ("cached", 0),
        ("loaded_status", 0),
        ("load", 0),
        ("client", 0),
        ("chat", 1),
        ("usage", 1),
    ),
)
def test_unpinned_inference_rejects_mid_attempt_variant_switch(
    phase: str, chat_calls: int
) -> None:
    model = _Model(phase)
    provider = _provider(model)
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))
    _assert_identity_failure(caught.value)
    assert model.chat_calls == chat_calls
    if phase in ("cached", "loaded_status"):
        assert model.load_calls == 0
    if phase == "load":
        assert model.load_calls == 1
        assert model.unload_calls == 1
        assert model.is_loaded is False


def test_unpinned_acquisition_rejects_variant_switch_after_download() -> None:
    model = _Model("download")
    provider = _provider(model)
    authorization = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="approved-alias",
        license_reference="qa-reviewed-model-license",
    )
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.download_model(authorization, timeout_seconds=2.0))
    _assert_identity_failure(caught.value)
    assert model.download_calls == 1


def test_unpinned_acquisition_rejects_switch_before_native_download() -> None:
    model = _Model("pre_download")
    provider = _provider(model)
    authorization = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="approved-alias",
        license_reference="qa-reviewed-model-license",
    )
    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.download_model(authorization, timeout_seconds=2.0))
    _assert_identity_failure(caught.value)
    assert model.download_calls == 0


def test_unpinned_inspection_rejects_variant_switch_during_evidence() -> None:
    model = _Model("evidence")
    provider = _provider(model)
    with pytest.raises(ModelGatewayError) as caught:
        provider.inspect_model("approved-alias")
    _assert_identity_failure(caught.value)


def test_stable_unpinned_variant_remains_usable() -> None:
    model = _Model()
    provider = _provider(model)
    result = asyncio.run(provider.complete(_request()))
    evidence = provider.inspect_model("approved-alias")
    assert result.text == "готово"
    assert result.model == "approved-alias"
    assert evidence.model_id == "stable-artifact:1"
    assert evidence.cached is True
    provider.close()
    assert model.unload_calls == 1
