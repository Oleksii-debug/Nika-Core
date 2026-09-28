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
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Model:
    def __init__(self) -> None:
        self.id: object = "strict-model-cpu:1"
        self.alias: object = "strict-model"
        self.is_cached: object = True
        self.is_loaded: object = False
        self.context_length: object = 4096
        self.input_modalities: object = "text"
        self.output_modalities: object = "text"
        self.capabilities: object = "chat"
        self.supports_tool_calling: object = False
        self.load_count = 0
        self.download_count = 0
        self.completion_count = 0

    def get_path(self) -> str:
        return "C:/Nika Test Models/strict-model"

    def load(self) -> None:
        self.load_count += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def download(self, *, cancel_event: object) -> None:
        del cancel_event
        self.download_count += 1
        self.is_cached = True

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                model.completion_count += 1
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
    def __init__(self, model: _Model) -> None:
        self._model = model

    def get_model(self, alias: str) -> _Model | None:
        return self._model if alias == "strict-model" else None


class _Manager:
    def __init__(self, model: _Model) -> None:
        self.catalog = _Catalog(model)


def _provider(model: _Model) -> FoundryLocalProvider:
    return FoundryLocalProvider(
        default_model="strict-model",
        manager_factory=lambda: _Manager(model),
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="strict-sdk-metadata",
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=1.0,
    )


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="strict-model",
        license_reference="MODEL-LICENSE-REVIEW-STRICT-METADATA",
        expected_model_id="strict-model-cpu:1",
    )


def _assert_provider_error(exc_info: pytest.ExceptionInfo[ModelGatewayError]) -> None:
    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.retryable is False


def test_string_cached_flag_cannot_bypass_explicit_download_before_inference() -> None:
    model = _Model()
    model.is_cached = "false"

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(_provider(model).complete(_request()))

    _assert_provider_error(exc_info)
    assert model.load_count == 0
    assert model.completion_count == 0


def test_string_cached_flag_cannot_publish_cached_download_evidence() -> None:
    model = _Model()
    model.is_cached = "false"

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(_provider(model).download_model(_authorization(), timeout_seconds=1.0))

    _assert_provider_error(exc_info)
    assert model.download_count == 0


def test_string_loaded_flag_cannot_skip_provider_owned_load_boundary() -> None:
    model = _Model()
    model.is_loaded = "false"

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(_provider(model).complete(_request()))

    _assert_provider_error(exc_info)
    assert model.load_count == 0
    assert model.completion_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 123),
        ("alias", 456),
        ("context_length", "4096"),
        ("input_modalities", 1),
        ("output_modalities", 1),
        ("capabilities", 1),
    ],
)
def test_model_metadata_is_not_coerced_into_plausible_evidence(
    field: str,
    value: object,
) -> None:
    model = _Model()
    setattr(model, field, value)

    with pytest.raises(ModelGatewayError) as exc_info:
        _provider(model).inspect_model()

    _assert_provider_error(exc_info)
